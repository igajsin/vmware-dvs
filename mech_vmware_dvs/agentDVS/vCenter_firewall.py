# Copyright 2015 Mirantis, Inc.
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from neutron.agent import firewall
from neutron.common import constants
from neutron.i18n import _LI, _LW
from oslo_log import log as logging

from mech_vmware_dvs import config
from mech_vmware_dvs import security_group_utils as sg_util
from mech_vmware_dvs import util
LOG = logging.getLogger(__name__)

CONF = config.CONF


class DVSFirewallDriver(firewall.FirewallDriver):
    """DVS Firewall Driver.
    """
    def __init__(self):
        self.networking_map = util.create_network_map_from_config(
            CONF.ml2_vmware)
        self.dvs_ports = {}
        self.sg_rules = {}
        self.sg_members = {}
        self._pre_defer_dvs_ports = None
        self.pre_sg_rules = None
        self.pre_sg_members = None
        self._defer_apply = False
        # Map for known ports and dvs it is connected to.
        self.dvs_port_map = {}

    @util.wrap_retry
    def prepare_port_filter(self, port):
        self.dvs_ports[port['device']] = port
        self._apply_sg_rules_for_port(port)
        LOG.info(_LI("Applied security group rules for port %s"), port['id'])

    def apply_port_filter(self, port):
        self.dvs_ports[port['device']] = port
        # Called for setting port in dvs_port_map
        self._get_dvs_for_port_id(port['id'])

    @util.wrap_retry
    def update_port_filter(self, port):
        self.dvs_ports[port['device']] = port
        self._apply_sg_rules_for_port(port)
        LOG.info(_LI("Updated security group rules for port %s"), port['id'])

    def remove_port_filter(self, port):
        self._remove_sg_from_dvs_port(port)
        self.dvs_ports.pop(port['device'], None)
        for ports in self.dvs_port_map.values():
            ports.discard(port['id'])

    @property
    def ports(self):
        return self.dvs_ports

    @util.wrap_retry
    def update_security_group_rules(self, sg_id, sg_rules):
        if sg_id in self.sg_rules and self.sg_rules[sg_id] == sg_rules:
            return
        elif sg_id in self.sg_rules:
            # For remote sg rules we need to apply ip_sets manually
            sg_rules = self._apply_ip_set(sg_rules)
            if self.sg_rules[sg_id] == sg_rules:
                return
        self.sg_rules[sg_id] = sg_rules
        self._update_sg_rules_for_ports([sg_id])
        LOG.debug("Update rules of security group (%s)", sg_id)

    @util.wrap_retry
    def update_security_group_members(self, sg_id, sg_members):
        updated = False
        updated_sgs = set(sg_id)
        for sg, rules in self.sg_rules.items():
            for rule in rules:
                if rule.get('remote_group_id') == sg_id:
                    ethertype = rule['ethertype']
                    if (sg_members.get(ethertype) and
                            rule.get('ip_set') != sg_members[ethertype]):
                        rule['ip_set'] = sg_members[ethertype]
                        updated = True
                        if sg_id != sg:
                            updated_sgs.add(sg)
        if updated:
            self._update_sg_rules_for_ports(updated_sgs)
        self.sg_members[sg_id] = sg_members
        LOG.debug("Update members of security group (%s)", sg_id)

    def security_group_updated(self, action_type, sec_group_ids,
                               device_id=None):
        pass

    def _apply_ip_set(self, rules):
        for rule in rules:
            for sg_id, members in self.sg_members.iteritems():
                if rule.get('remote_group_id') == sg_id:
                    ethertype = rule['ethertype']
                    if members.get(ethertype):
                        rule['ip_set'] = members[ethertype]
        return rules

    def _apply_sg_rules_for_port(self, port):
        dev = port['device']
        sg_rules = 'security_group_rules'
        for sg in port['security_groups']:
            if sg in self.sg_rules.keys():
                if (port['id'] not in self.dvs_port_map.keys() or
                        self.dvs_ports[dev][sg_rules] != self.sg_rules[sg]):
                    port['security_group_rules'] = self.sg_rules[sg]
        # TODO(akamyshnikova): improve applying rules in case of agent restart
        if port['security_group_rules']:
            dvs = self._get_dvs_for_port_id(port['id'])
            if dvs:
                sg_util.update_port_rules(dvs, [port])

    def _get_dvs_for_port_id(self, port_id):
        if port_id not in self.dvs_port_map.keys():
            port_map = util.create_port_map(self.networking_map.values())
        else:
            port_map = self.dvs_port_map
        for dvs, port_list in port_map.iteritems():
            if port_id in port_list:
                if dvs not in self.dvs_port_map:
                    self.dvs_port_map[dvs] = set()
                self.dvs_port_map[dvs].add(port_id)
                return dvs
            else:
                LOG.warning(_LW("Can find dvs for port %s"), port_id)

    def _update_sg_rules_for_ports(self, sg_ids):
        ports_to_update = []
        for port in self.dvs_ports.values():
            for sg_id in sg_ids:
                if sg_id in port['security_groups']:
                    port['security_group_rules'] = self.sg_rules[sg_id]
                    ports_to_update.append(port)
        port_ids = {p['id']: p for p in ports_to_update}
        for dvs, port_list in self.dvs_port_map.iteritems():
            p = [port_ids[id] for id in port_list if id in port_ids.keys()]
            if p:
                sg_util.update_port_rules(dvs, p)

    def _remove_sg_from_dvs_port(self, port):
        port['security_group_rules'] = []
        dvs = self._get_dvs_for_port_id(port['id'])
        if dvs:
            sg_util.update_port_rules(self._get_dvs_for_port_id(port['id']),
                                      [port])

    def filter_defer_apply_on(self):
        if not self._defer_apply:
            self._pre_defer_dvs_ports = dict(self.dvs_ports)
            self.pre_sg_members = dict(self.sg_members)
            self.pre_sg_rules = dict(self.sg_rules)
            self._defer_apply = True

    def _remove_unused_security_group_info(self):
        """Remove any unnecessary local security group info.
        """
        dvs_ports = self.dvs_ports.values()

        remote_sgs_to_remove = self._determine_remote_sgs_to_remove(
            dvs_ports)

        for ip_version, remote_sg_ids in remote_sgs_to_remove.iteritems():
            self._clear_sg_members(ip_version, remote_sg_ids)

        self._remove_unused_sg_members()

        # Remove unused security group rules
        for remove_group_id in self._determine_sg_rules_to_remove(
                dvs_ports):
            self.sg_rules.pop(remove_group_id, None)

    def _determine_remote_sgs_to_remove(self, dvs_ports):
        """Calculate which remote security groups we don't need anymore.
        We do the calculation for each ip_version.
        """
        sgs_to_remove_per_ipversion = {constants.IPv4: set(),
                                       constants.IPv6: set()}
        remote_group_id_sets = self._get_remote_sg_ids_sets_by_ipversion(
            dvs_ports)
        for ip_version, remote_group_id_set in (
            remote_group_id_sets.iteritems()):
            sgs_to_remove_per_ipversion[ip_version].update(
                set(self.pre_sg_members) - remote_group_id_set)
        return sgs_to_remove_per_ipversion

    def _get_remote_sg_ids_sets_by_ipversion(self, dvs_ports):
        """Given a port, calculates the remote sg references by ip_version."""
        remote_group_id_sets = {constants.IPv4: set(),
                                constants.IPv6: set()}
        for port in dvs_ports:
            remote_sg_ids = self._get_remote_sg_ids(port)
            for ip_version in (constants.IPv4, constants.IPv6):
                remote_group_id_sets[ip_version] |= remote_sg_ids[ip_version]
        return remote_group_id_sets

    def _determine_sg_rules_to_remove(self, dvs_ports):
        """Calculate which security groups need to be removed.
        We find out by subtracting our previous sg group ids,
        with the security groups associated to a set of ports.
        """
        port_group_ids = self._get_sg_ids_set_for_ports(dvs_ports)
        return set(self.pre_sg_rules) - port_group_ids

    def _get_sg_ids_set_for_ports(self, dvs_ports):
        """Get the port security group ids as a set."""
        port_group_ids = set()
        for port in dvs_ports:
            port_group_ids.update(port.get('security_groups', []))
        return port_group_ids

    def _clear_sg_members(self, ip_version, remote_sg_ids):
        """Clear our internal cache of sg members matching the parameters."""
        for remote_sg_id in remote_sg_ids:
            if self.sg_members[remote_sg_id][ip_version]:
                self.sg_members[remote_sg_id][ip_version] = []

    def _remove_unused_sg_members(self):
        """Remove sg_member entries where no IPv4 or IPv6 is associated."""
        for sg_id in self.sg_members.keys():
            sg_has_members = (self.sg_members[sg_id].get(constants.IPv4) or
                              self.sg_members[sg_id].get(constants.IPv6))
            if not sg_has_members:
                del self.sg_members[sg_id]

    def _get_remote_sg_ids(self, port, direction=None):
        sg_ids = port.get('security_groups', [])
        remote_sg_ids = {constants.IPv4: set(), constants.IPv6: set()}
        for sg_id in sg_ids:
            for rule in self.sg_rules.get(sg_id, []):
                if not direction or rule['direction'] == direction:
                    remote_sg_id = rule.get('remote_group_id')
                    ether_type = rule.get('ethertype')
                    if remote_sg_id and ether_type:
                        remote_sg_ids[ether_type].add(remote_sg_id)
        return remote_sg_ids

    def filter_defer_apply_off(self):
        if self._defer_apply:
            self._defer_apply = False
            self._remove_unused_security_group_info()
            self._pre_defer_dvs_ports = None