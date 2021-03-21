#!/usr/bin/env python3
#
# Copyright (C) 2020-2021 VyOS maintainers and contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os

from sys import exit
from sys import argv

from vyos.config import Config
from vyos.configdict import node_changed
from vyos.util import call
from vyos.util import dict_search
from vyos.util import get_interface_config
from vyos.template import render_to_string
from vyos import ConfigError
from vyos import frr
from vyos import airbag
airbag.enable()

frr_daemon = 'isisd'

def get_config(config=None):
    if config:
        conf = config
    else:
        conf = Config()

    vrf = None
    if len(argv) > 1:
        vrf = argv[1]

    base_path = ['protocols', 'isis']

    # eqivalent of the C foo ? 'a' : 'b' statement
    base = vrf and ['vrf', 'name', vrf, 'protocols', 'isis'] or base_path
    isis = conf.get_config_dict(base, key_mangling=('-', '_'),
                                get_first_key=True)

    # Assign the name of our VRF context. This MUST be done before the return
    # statement below, else on deletion we will delete the default instance
    # instead of the VRF instance.
    if vrf: isis['vrf'] = vrf

    # As we no re-use this Python handler for both VRF and non VRF instances for
    # IS-IS we need to find out if any interfaces changed so properly adjust
    # the FRR configuration and not by acctident change interfaces from a
    # different VRF.
    interfaces_removed = node_changed(conf, base + ['interface'])
    if interfaces_removed:
        isis['interface_removed'] = list(interfaces_removed)

    # Bail out early if configuration tree does not exist
    if not conf.exists(base):
        isis.update({'deleted' : ''})
        return isis

    return isis

def verify(isis):
    # bail out early - looks like removal from running config
    if not isis or 'deleted' in isis:
        return None

    if 'domain' not in isis:
        raise ConfigError('Routing domain name/tag must be set!')

    if 'net' not in isis:
        raise ConfigError('Network entity is mandatory!')

    # If interface not set
    if 'interface' not in isis:
        raise ConfigError('Interface used for routing updates is mandatory!')

    for interface in isis['interface']:
        if 'vrf' in isis:
            # If interface specific options are set, we must ensure that the
            # interface is bound to our requesting VRF. Due to the VyOS
            # priorities the interface is bound to the VRF after creation of
            # the VRF itself, and before any routing protocol is configured.
            vrf = isis['vrf']
            tmp = get_interface_config(interface)
            if 'master' not in tmp or tmp['master'] != vrf:
                raise ConfigError(f'Interface {interface} is not a member of VRF {vrf}!')

    # If md5 and plaintext-password set at the same time
    if 'area_password' in isis:
        if {'md5', 'plaintext_password'} <= set(isis['encryption']):
            raise ConfigError('Can not use both md5 and plaintext-password for ISIS area-password!')

    # If one param from delay set, but not set others
    if 'spf_delay_ietf' in isis:
        required_timers = ['holddown', 'init_delay', 'long_delay', 'short_delay', 'time_to_learn']
        exist_timers = []
        for elm_timer in required_timers:
            if elm_timer in isis['spf_delay_ietf']:
                exist_timers.append(elm_timer)

        exist_timers = set(required_timers).difference(set(exist_timers))
        if len(exist_timers) > 0:
            raise ConfigError('All types of delay must be specified: ' + ', '.join(exist_timers).replace('_', '-'))

    # If Redistribute set, but level don't set
    if 'redistribute' in isis:
        proc_level = isis.get('level','').replace('-','_')
        for afi in ['ipv4']:
            if afi not in isis['redistribute']:
                continue

            for proto, proto_config in isis['redistribute'][afi].items():
                if 'level_1' not in proto_config and 'level_2' not in proto_config:
                    raise ConfigError(f'Redistribute level-1 or level-2 should be specified in ' \
                                      f'"protocols isis {process} redistribute {afi} {proto}"!')

                for redistr_level, redistr_config in proto_config.items():
                    if proc_level and proc_level != 'level_1_2' and proc_level != redistr_level:
                        raise ConfigError(f'"protocols isis {process} redistribute {afi} {proto} {redistr_level}" ' \
                                          f'can not be used with \"protocols isis {process} level {proc_level}\"')

    # Segment routing checks
    if dict_search('segment_routing.global_block', isis):
        high_label_value = dict_search('segment_routing.global_block.high_label_value', isis)
        low_label_value = dict_search('segment_routing.global_block.low_label_value', isis)

        # If segment routing global block high value is blank, throw error
        if (low_label_value and not high_label_value) or (high_label_value and not low_label_value):
            raise ConfigError('Segment routing global block requires both low and high value!')

        # If segment routing global block low value is higher than the high value, throw error
        if int(low_label_value) > int(high_label_value):
            raise ConfigError('Segment routing global block low value must be lower than high value')

    if dict_search('segment_routing.local_block', isis):
        high_label_value = dict_search('segment_routing.local_block.high_label_value', isis)
        low_label_value = dict_search('segment_routing.local_block.low_label_value', isis)

        # If segment routing local block high value is blank, throw error
        if (low_label_value and not high_label_value) or (high_label_value and not low_label_value):
            raise ConfigError('Segment routing local block requires both high and low value!')

        # If segment routing local block low value is higher than the high value, throw error
        if int(low_label_value) > int(high_label_value):
            raise ConfigError('Segment routing local block low value must be lower than high value')

    return None

def generate(isis):
    if not isis or 'deleted' in isis:
        isis['new_frr_config'] = ''
        return None

    isis['new_frr_config'] = render_to_string('frr/isis.frr.tmpl', isis)
    return None

def apply(isis):
    # Save original configuration prior to starting any commit actions
    frr_cfg = frr.FRRConfig()
    frr_cfg.load_configuration(frr_daemon)

    # Generate empty helper string which can be ammended to FRR commands,
    # it will be either empty (default VRF) or contain the "vrf <name" statement
    vrf = ''
    if 'vrf' in isis:
        vrf = ' vrf ' + isis['vrf']

    frr_cfg.modify_section(f'^router isis \S+{vrf}$', '')
    for key in ['interface', 'interface_removed']:
        if key not in isis:
            continue
        for interface in isis[key]:
            frr_cfg.modify_section(f'^interface {interface}{vrf}$', '')

    frr_cfg.add_before(r'(ip prefix-list .*|route-map .*|line vty)', isis['new_frr_config'])
    frr_cfg.commit_configuration(frr_daemon)

    # If FRR config is blank, rerun the blank commit x times due to frr-reload
    # behavior/bug not properly clearing out on one commit.
    if isis['new_frr_config'] == '':
        for a in range(5):
            frr_cfg.commit_configuration(frr_daemon)

    return None

if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        generate(c)
        apply(c)
    except ConfigError as e:
        print(e)
        exit(1)
