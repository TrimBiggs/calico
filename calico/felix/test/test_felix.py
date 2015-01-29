# -*- coding: utf-8 -*-
# Copyright 2014 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
felix.test.test_felix
~~~~~~~~~~~

Top level tests for Felix.
"""
import logging
import mock
import socket
import sys
import time
import unittest
import uuid

import calico.felix.futils as futils

# Import our stub utils module which replaces time etc.
import calico.felix.test.stub_utils as stub_utils

# Replace zmq with our stub zmq.
import calico.felix.test.stub_zmq as stub_zmq
from calico.felix.test.stub_zmq import (TYPE_EP_REQ, TYPE_EP_REP,
                                        TYPE_ACL_REQ, TYPE_ACL_SUB)
sys.modules['zmq'] = stub_zmq

# Hide iptc, since we do not have it.
sys.modules['iptc'] = __import__('calico.felix.test.stub_empty')

# Replace calico.felix.fiptables with calico.felix.test.stub_fiptables
import calico.felix.test.stub_fiptables
sys.modules['calico.felix.fiptables'] = __import__('calico.felix.test.stub_fiptables')
calico.felix.fiptables = calico.felix.test.stub_fiptables
stub_fiptables = calico.felix.test.stub_fiptables

#*****************************************************************************#
#* Load calico.felix.devices and calico.felix.test.stub_devices, and the     *#
#* same for ipsets; we do not blindly override as we need to avoid getting   *#
#* into a state where tests of these modules cannot be made to work.         *#
#*****************************************************************************#
import calico.felix.devices
import calico.felix.test.stub_devices as stub_devices
import calico.felix.ipsets
import calico.felix.test.stub_ipsets as stub_ipsets

# Now import felix, and away we go.
import calico.felix.felix as felix
import calico.felix.endpoint as endpoint
import calico.felix.frules as frules
import calico.common as common
from calico.felix.futils import IPV4, IPV6
from calico.felix.endpoint import Endpoint
from calico.felix.fsocket import Socket 

# IPtables state.
expected_iptables = stub_fiptables.TableState()
expected_ipsets = stub_ipsets.IpsetState()

# Default config path.
config_path = "calico/felix/test/data/felix_debug.cfg"

# Logger
log = logging.getLogger(__name__)

class TestBasic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Completely replace the devices and ipsets modules.
        cls.real_devices = calico.felix.devices
        endpoint.devices = stub_devices
        cls.real_ipsets = calico.felix.ipsets
        frules.ipsets = stub_ipsets

    @classmethod
    def tearDownClass(cls):
        # Reinstate the modules we overwrote
        endpoint.devices = cls.real_devices
        frules.ipsets = cls.real_ipsets

    def create_patch(self, name):
        return thing

    def setUp(self):
        # Mock out time
        patcher = mock.patch('calico.felix.futils.time_ms')
        patcher.start().side_effect = stub_utils.get_time
        self.addCleanup(patcher.stop)
        
        stub_utils.set_time(0)
        stub_fiptables.reset_current_state()
        stub_devices.reset()
        stub_ipsets.reset()

        expected_iptables.reset()
        expected_ipsets.reset()

    def tearDown(self):
        pass

    def test_startup(self):
        common.default_logging()
        context = stub_zmq.Context()
        agent = felix.FelixAgent(config_path, context)

        set_expected_global_rules()
        stub_fiptables.check_state(expected_iptables)
        stub_ipsets.check_state(expected_ipsets)

        self.assertEqual(agent.hostname, "test_hostname")

    def test_no_work(self):
        """
        Test starting up, and sending no work at all.
        """
        common.default_logging()
        context = stub_zmq.Context()
        agent = felix.FelixAgent(config_path, context)
        context.add_poll_result(0)
        agent.run()

        set_expected_global_rules()
        stub_fiptables.check_state(expected_iptables)
        stub_ipsets.check_state(expected_ipsets)

    def test_main_flow(self):
        """
        Test starting up and going through some of the basic flow.
        """
        common.default_logging()
        context = stub_zmq.Context()
        agent = felix.FelixAgent(config_path, context)
        context.add_poll_result(0)
        agent.run()

        # Now we want to reply to the RESYNC request.
        resync_req = context.sent_data[TYPE_EP_REQ].pop()
        log.debug("Resync request : %s" % resync_req)
        self.assertFalse(context.sent_data_present())
        resync_id = resync_req['resync_id']
        resync_rsp = { 'type': "RESYNCSTATE",
                       'endpoint_count': 1,
                       'rc': "SUCCESS",
                       'message': "hello" }

        poll_result = context.add_poll_result(50)
        poll_result.add(TYPE_EP_REQ, resync_rsp)
        agent.run()

        # Felix expects one endpoint created message - give it what it wants
        addr = "1.2.3.4"
        endpoint = CreatedEndpoint([addr])
        log.debug("Build first endpoint created : %s", endpoint.id)
        poll_result = context.add_poll_result(100)
        poll_result.add(TYPE_EP_REP, endpoint.create_req)
        agent.run()

        log.debug("Create tap interface %s", endpoint.tap)
        poll_result = context.add_poll_result(150)
        agent.run()

        #*********************************************************************#
        #* As soon as that endpoint has been made to exist, we should see an *#
        #* ACL request coming through, and a response to the endpoint        *#
        #* created.  We send a reply to that now.                            *#
        #*********************************************************************#
        endpoint_created_rsp = context.sent_data[TYPE_EP_REP].pop()
        self.assertEqual(endpoint_created_rsp['rc'], "SUCCESS")

        acl_req = context.sent_data[TYPE_ACL_REQ].pop()
        self.assertFalse(context.sent_data_present())
        self.assertEqual(acl_req['endpoint_id'], endpoint.id)

        acl_rsp = { 'type': "GETACLSTATE",
                    'rc': "SUCCESS",
                    'message': "" }
        poll_result = context.add_poll_result(200)
        poll_result.add(TYPE_ACL_REQ, acl_rsp)

        # Check the rules are what we expect.
        set_expected_global_rules()
        add_endpoint_rules(endpoint.suffix, endpoint.tap, addr, None, endpoint.mac)
        stub_fiptables.check_state(expected_iptables)
        add_endpoint_ipsets(endpoint.suffix)
        stub_ipsets.check_state(expected_ipsets)

        # OK - now try giving it some ACLs, and see if they get applied correctly.
        acls = get_blank_acls()
        acls['v4']['outbound'].append({ 'cidr': "0.0.0.0/0", 'protocol': "icmp" })
        acls['v4']['outbound'].append({ 'cidr': "1.2.3.0/24", 'protocol': "tcp" })
        acls['v4']['outbound'].append({ 'cidr': "0.0.0.0/0", 'protocol': "tcp", 'port': "80" })
        acls['v4']['inbound'].append({ 'cidr': "1.2.2.0/24", 'protocol': "icmp" })
        acls['v4']['inbound'].append({ 'cidr': "0.0.0.0/0", 'protocol': "tcp", 'port': "8080" })
        acls['v4']['inbound'].append({ 'cidr': "2.4.6.8/32", 'protocol': "udp", 'port': "8080" })
        acls['v4']['inbound'].append({ 'cidr': "1.2.3.3/32" })
        acls['v4']['inbound'].append({ 'cidr': "3.6.9.12/32",
                                       'protocol': "tcp",
                                       'port': ['10', '50'] })

        acls['v4']['inbound'].append({ 'cidr': "5.4.3.2/32",
                                       'protocol': "icmp",
                                       'icmp_type': "3",
                                       'icmp_code': "2" })

        acls['v4']['inbound'].append({ 'cidr': "5.4.3.2/32",
                                       'protocol': "icmp",
                                       'icmp_type': "9" })

        acls['v4']['inbound'].append({ 'cidr': "5.4.3.2/32",
                                       'protocol': "icmp",
                                       'icmp_type': "blah" })

        # We include a couple of invalid rules that Felix will just ignore (and log).
        acls['v4']['inbound'].append({ 'cidr': "4.3.2.1/32",
                                       'protocol': "tcp",
                                       'port': ['blah', 'blah'] })
        acls['v4']['inbound'].append({ 'cidr': "4.3.2.1/32",
                                       'protocol': "tcp",
                                       'port': ['1', '2', '3'] })
        acls['v4']['inbound'].append({ 'cidr': "4.3.2.1/32",
                                       'protocol': "tcp",
                                       'port': 'flibble' })
        acls['v4']['inbound'].append({ 'protocol': "tcp" })
        acls['v4']['inbound'].append({ 'cidr': "4.3.2.1/32",
                                       'port': "123" })
        acls['v4']['inbound'].append({ 'cidr': "4.3.2.1/32",
                                       'protocol': "icmp",
                                       'icmp_code': "blah" })
        acls['v4']['inbound'].append({ 'cidr': "4.3.2.1/32",
                                       'protocol': "icmp",
                                       'port': "1" })
        acls['v4']['inbound'].append({ 'cidr': "4.3.2.1/32",
                                       'protocol': "rsvp",
                                       'port': "1" })

        acl_req = { 'type': "ACLUPDATE",
                    'acls': acls }

        poll_result.add(TYPE_ACL_SUB, acl_req, endpoint.id)
        agent.run()

        stub_fiptables.check_state(expected_iptables)
        expected_ipsets.add("felix-from-icmp-" + endpoint.suffix, "0.0.0.0/1")
        expected_ipsets.add("felix-from-icmp-" + endpoint.suffix, "128.0.0.0/1")
        expected_ipsets.add("felix-from-port-" + endpoint.suffix, "1.2.3.0/24,tcp:0")
        expected_ipsets.add("felix-from-port-" + endpoint.suffix, "0.0.0.0/1,tcp:80")
        expected_ipsets.add("felix-from-port-" + endpoint.suffix, "128.0.0.0/1,tcp:80")

        expected_ipsets.add("felix-to-icmp-" + endpoint.suffix, "1.2.2.0/24")
        expected_ipsets.add("felix-to-port-" + endpoint.suffix, "0.0.0.0/1,tcp:8080")
        expected_ipsets.add("felix-to-port-" + endpoint.suffix, "128.0.0.0/1,tcp:8080")
        expected_ipsets.add("felix-to-port-" + endpoint.suffix, "2.4.6.8/32,udp:8080")
        expected_ipsets.add("felix-to-addr-" + endpoint.suffix, "1.2.3.3/32")
        expected_ipsets.add("felix-to-port-" + endpoint.suffix, "3.6.9.12/32,tcp:10-50")
        expected_ipsets.add("felix-to-port-" + endpoint.suffix, "5.4.3.2/32,icmp:3/2")
        expected_ipsets.add("felix-to-port-" + endpoint.suffix, "5.4.3.2/32,icmp:9/0")
        expected_ipsets.add("felix-to-port-" + endpoint.suffix, "5.4.3.2/32,icmp:blah")

        stub_ipsets.check_state(expected_ipsets)

        # Add another endpoint, and check the state.
        addr2 = "1.2.3.5"
        endpoint2 = CreatedEndpoint([addr2])
        log.debug("Build second endpoint created : %s", endpoint2.id)
        poll_result = context.add_poll_result(250)
        poll_result.add(TYPE_EP_REP, endpoint2.create_req)
        agent.run()

        # Check that we got what we expected - i.e. a success response, a GETACLSTATE,
        # and the rules in the right state.
        endpoint_created_rsp = context.sent_data[TYPE_EP_REP].pop()
        self.assertEqual(endpoint_created_rsp['rc'], "SUCCESS")

        acl_req = context.sent_data[TYPE_ACL_REQ].pop()
        self.assertEqual(acl_req['endpoint_id'], endpoint2.id)
        self.assertFalse(context.sent_data_present())

        add_endpoint_rules(endpoint2.suffix, endpoint2.tap, addr2, None, endpoint2.mac)
        stub_fiptables.check_state(expected_iptables)
        add_endpoint_ipsets(endpoint2.suffix)
        stub_ipsets.check_state(expected_ipsets)

        # OK, finally wind down with an ENDPOINTDESTROYED message for that second endpoint.
        poll_result = context.add_poll_result(300)
        poll_result.add(TYPE_EP_REP, endpoint2.destroy_req)
        stub_devices.del_tap(endpoint2.tap)
        agent.run()

        # Rebuild and recheck the state. Only the first endpoint still exists.
        set_expected_global_rules()
        add_endpoint_rules(endpoint.suffix, endpoint.tap, addr, None, endpoint.mac)
        stub_fiptables.check_state(expected_iptables)

    def test_rule_reordering(self):
        # TODO: Want to check that with extra rules, the extras get tidied up.
        pass

    def test_ipv6_reordering(self):
        # TODO: Want to test IP v6 addresses and rules too.
        pass


class TestTimings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Completely replace the devices and ipsets modules.
        cls.real_devices = calico.felix.devices
        endpoint.devices = stub_devices
        cls.real_ipsets = calico.felix.ipsets
        frules.ipsets = stub_ipsets

    @classmethod
    def tearDownClass(cls):
        # Reinstate the modules we overwrote
        endpoint.devices = cls.real_devices
        frules.ipsets = cls.real_ipsets

    def setUp(self):
        # Mock out time
        patcher = mock.patch('calico.felix.futils.time_ms')
        patcher.start().side_effect = stub_utils.get_time
        self.addCleanup(patcher.stop)
        
        stub_utils.set_time(0)
        stub_fiptables.reset_current_state()
        stub_devices.reset()
        stub_ipsets.reset()

        expected_iptables.reset()
        expected_ipsets.reset()

    def tearDown(self):
        pass

    def test_resync(self):
        """
        Test the resync flows.
        """
        common.default_logging()
        context = stub_zmq.Context()
        agent = felix.FelixAgent(config_path, context)

        #*********************************************************************#
        #* Set the resync timeout to 5 seconds, and the KEEPALIVE timeout to *#
        #* much more.                                                        *#
        #*********************************************************************#
        agent.config.RESYNC_INT_SEC = 5
        agent.config.CONN_TIMEOUT_MS = 50000
        agent.config.CONN_KEEPALIVE_MS = 50000

        # Get started.
        context.add_poll_result(0)
        agent.run()

        # Now we should have got a resync request.
        resync_req = context.sent_data[TYPE_EP_REQ].pop()
        log.debug("Resync request : %s" % resync_req)
        self.assertFalse(context.sent_data_present())
        resync_id = resync_req['resync_id']
        resync_rsp = { 'type': "RESYNCSTATE",
                       'endpoint_count': "0",
                       'rc': "SUCCESS",
                       'message': "hello" }

        poll_result = context.add_poll_result(1000)
        poll_result.add(TYPE_EP_REQ, resync_rsp)
        agent.run()
        # nothing yet
        self.assertFalse(context.sent_data_present())

        poll_result = context.add_poll_result(5999)
        agent.run()
        # nothing yet - 4999 ms since last request
        self.assertFalse(context.sent_data_present())

        poll_result = context.add_poll_result(6001)
        agent.run()

        # We should have got another resync request.
        resync_req = context.sent_data[TYPE_EP_REQ].pop()
        log.debug("Resync request : %s" % resync_req)
        self.assertFalse(context.sent_data_present())
        resync_id = resync_req['resync_id']
        resync_rsp = { 'type': "RESYNCSTATE",
                       'endpoint_count': "2",
                       'rc': "SUCCESS",
                       'message': "hello" }

        # No more resyncs until enough data has arrived.
        poll_result = context.add_poll_result(15000)
        poll_result.add(TYPE_EP_REQ, resync_rsp)
        agent.run()
        self.assertFalse(context.sent_data_present())
       
        # Send an endpoint created message to Felix.
        addr = '1.2.3.4'
        endpoint = CreatedEndpoint([addr], resync_id)
        log.debug("Build first endpoint created : %s", endpoint.id)
        poll_result = context.add_poll_result(15001)
        poll_result.add(TYPE_EP_REP, endpoint.create_req)
        agent.run()

        # We stop using sent_data_present, since there are ACL requests around.
        endpoint_created_rsp = context.sent_data[TYPE_EP_REP].pop()
        self.assertEqual(endpoint_created_rsp['rc'], "SUCCESS")
        self.assertFalse(context.sent_data[TYPE_EP_REQ])

        # Send a second endpoint created message to Felix - triggers another resync.
        addr = '1.2.3.5'
        endpoint2 = CreatedEndpoint([addr], resync_id)
        log.debug("Build second endpoint created : %s" % endpoint2.id)

        poll_result = context.add_poll_result(15002)
        poll_result.add(TYPE_EP_REP, endpoint2.create_req)
        agent.run()

        endpoint_created_rsp = context.sent_data[TYPE_EP_REP].pop()
        self.assertEqual(endpoint_created_rsp['rc'], "SUCCESS")
        self.assertFalse(context.sent_data[TYPE_EP_REQ])

        # No more resyncs until enough 5000 ms after last rsp.
        poll_result = context.add_poll_result(20000)
        poll_result.add(TYPE_EP_REQ, resync_rsp)
        agent.run()
        self.assertFalse(context.sent_data[TYPE_EP_REQ])

        # We should have got another resync request.
        poll_result = context.add_poll_result(20003)
        poll_result.add(TYPE_EP_REP, endpoint2.create_req)
        agent.run()
        resync_req = context.sent_data[TYPE_EP_REQ].pop()
        log.debug("Resync request : %s" % resync_req)
        self.assertFalse(context.sent_data[TYPE_EP_REQ])

    def test_keepalives(self):
        """
        Test that keepalives are sent.
        """
        common.default_logging()
        context = stub_zmq.Context()
        agent = felix.FelixAgent(config_path, context)

        agent.config.RESYNC_INT_SEC = 500
        agent.config.CONN_TIMEOUT_MS = 50000
        agent.config.CONN_KEEPALIVE_MS = 5000

        # Get started.
        context.add_poll_result(0)
        agent.run()

        # Now we should have got a resync request.
        resync_req = context.sent_data[TYPE_EP_REQ].pop()
        log.debug("Resync request : %s" % resync_req)
        self.assertFalse(context.sent_data_present())
        resync_id = resync_req['resync_id']
        resync_rsp = { 'type': "RESYNCSTATE",
                       'endpoint_count': "0",
                       'rc': "SUCCESS",
                       'message': "hello" }

        # We should send keepalives on the 5 second boundary.
        poll_result = context.add_poll_result(4999)
        agent.run()
        self.assertFalse(context.sent_data_present())

        poll_result = context.add_poll_result(5001)
        agent.run()
        keepalive = context.sent_data[TYPE_ACL_REQ].pop()
        self.assertTrue(keepalive['type'] == "HEARTBEAT")
        self.assertFalse(context.sent_data_present())

        # Send the resync response now
        poll_result = context.add_poll_result(6000)
        poll_result.add(TYPE_EP_REQ, resync_rsp)
        agent.run()
        self.assertFalse(context.sent_data_present())

        # At time 9000, send the ACL response.
        poll_result = context.add_poll_result(9000)
        poll_result.add(TYPE_ACL_REQ,
                        {'type': "HEARTBEAT", 'rc': "SUCCESS"})
        agent.run()
        self.assertFalse(context.sent_data_present())
      
        # Now we should get another keepalive sent at 14 seconds on ACL_REQ,
        # and 11 on EP_REQ
        poll_result = context.add_poll_result(11001)
        agent.run()
        keepalive = context.sent_data[TYPE_EP_REQ].pop()
        self.assertTrue(keepalive['type'] == "HEARTBEAT")
        self.assertFalse(context.sent_data_present())

        poll_result = context.add_poll_result(14001)
        agent.run()
        keepalive = context.sent_data[TYPE_ACL_REQ].pop()
        self.assertTrue(keepalive['type'] == "HEARTBEAT")
        self.assertFalse(context.sent_data_present())

    def test_timeouts(self):
        """
        Test that connections time out correctly.
        """
        common.default_logging()
        context = stub_zmq.Context()
        agent = felix.FelixAgent(config_path, context)

        agent.config.RESYNC_INT_SEC = 500
        agent.config.CONN_TIMEOUT_MS = 50000
        agent.config.CONN_KEEPALIVE_MS = 5000

        # Get started.
        context.add_poll_result(0)
        agent.run()

        sock_zmq = {}
        for sock in agent.sockets.values():
            sock_zmq[sock] = sock._zmq

        # Now we should have got a resync request.
        resync_req = context.sent_data[TYPE_EP_REQ].pop()
        log.debug("Resync request : %s" % resync_req)
        self.assertFalse(context.sent_data_present())
        resync_id = resync_req['resync_id']
        resync_rsp = { 'type': "RESYNCSTATE",
                       'endpoint_count': "0",
                       'rc': "SUCCESS",
                       'message': "hello" }

        # Send keepalives on the connections that expect them
        poll_result = context.add_poll_result(0)
        poll_result.add(TYPE_EP_REQ, resync_rsp)
        poll_result.add(TYPE_EP_REP, {'type': "HEARTBEAT"})
        poll_result.add(TYPE_ACL_SUB, {'type': "HEARTBEAT"}, 'aclheartbeat')
        agent.run()

        # Give EP REQ a chance to send a keepalive.
        context.add_poll_result(10000)
        agent.run()

        # OK, so now we have some live connections. We let the EP REQ fail
        # first.
        context.add_poll_result(10000)
        agent.run()
        msg = context.sent_data[TYPE_EP_REQ].pop()
        self.assertEqual(msg['type'], "HEARTBEAT")
        msg = context.sent_data[TYPE_EP_REP].pop()
        self.assertEqual(msg['type'], "HEARTBEAT")
        msg = context.sent_data[TYPE_ACL_REQ].pop()
        self.assertEqual(msg['type'], "HEARTBEAT")
        self.assertFalse(context.sent_data_present())

        # And another 20 seconds
        poll_result = context.add_poll_result(40000)
        poll_result.add(TYPE_EP_REP, {'type': "HEARTBEAT"})
        poll_result.add(TYPE_ACL_SUB, {'type': "HEARTBEAT"}, 'aclheartbeat')
        poll_result.add(TYPE_ACL_REQ,
                        {'type': "HEARTBEAT", 'rc': "SUCCESS"})
        agent.run()
        for sock in agent.sockets.values():
            # Assert no connections have been restarted.
            self.assertIs(sock_zmq[sock], sock._zmq)

        # And another - which should lead to EP REQ going pop, and keepalives.
        context.add_poll_result(60000)
        agent.run()
        for sock in agent.sockets.values():
            if sock.type == TYPE_EP_REQ:
                self.assertIsNot(sock_zmq[sock], sock._zmq)
                sock_zmq[sock] = sock._zmq
            else:
                self.assertIs(sock_zmq[sock], sock._zmq)

        msg = context.sent_data[TYPE_EP_REP].pop()
        self.assertEqual(msg['type'], "HEARTBEAT")
        msg = context.sent_data[TYPE_ACL_REQ].pop()
        self.assertEqual(msg['type'], "HEARTBEAT")
        self.assertFalse(context.sent_data_present())

        # That connection that came up should be sending keepalives
        context.add_poll_result(70000)
        agent.run()
        msg = context.sent_data[TYPE_EP_REQ].pop()
        self.assertEqual(msg['type'], "HEARTBEAT")
        self.assertFalse(context.sent_data_present())

        # OK, so now time out the EP REP socket. This triggers a resync.
        poll_result = context.add_poll_result(80000)
        poll_result.add(TYPE_EP_REQ,
                        {'type': "HEARTBEAT", 'rc': "SUCCESS"})
        poll_result.add(TYPE_ACL_SUB, {'type': "HEARTBEAT"}, 'aclheartbeat')
        poll_result.add(TYPE_ACL_REQ,
                        {'type': "HEARTBEAT", 'rc': "SUCCESS"})
        agent.run()

        # This is the point where the EP REP socket is going to die.
        log.debug("EP REP should now trigger resync")
        poll_result = context.add_poll_result(120000)
        agent.run()
        msg = context.sent_data[TYPE_EP_REQ].pop()
        self.assertEqual(msg['type'], "RESYNCSTATE")
        resync_id = msg['resync_id']
        msg = context.sent_data[TYPE_ACL_REQ].pop()
        self.assertEqual(msg['type'], "HEARTBEAT")

        for sock in agent.sockets.values():
            # Assert no connections have been restarted.
            self.assertIs(sock_zmq[sock], sock._zmq)

        # OK, so send some messages in response.
        poll_request = context.add_poll_result(120000)
        resync_rsp = { 'type': "RESYNCSTATE",
                       'endpoint_count': "5",
                       'rc': "SUCCESS",
                       'message': "hello" }

        poll_result.add(TYPE_EP_REQ, resync_rsp)
        agent.run()

        addrs = [ "2001::%s" + str(i) for i in range(1,6) ]
        endpoints = []
        for addr in addrs:
            endpoint = CreatedEndpoint([addr], resync_id)
            endpoints.append(endpoint)

            poll_result = context.add_poll_result(120000)
            poll_result.add(TYPE_EP_REP, endpoint.create_req)
            agent.run()

            log.debug("Messages now : %s", context.sent_data)
            endpoint_created_rsp = context.sent_data[TYPE_EP_REP].pop()
            self.assertEqual(endpoint_created_rsp['rc'], "SUCCESS")

        # OK, so get the first two ACL state messages, blocked behind the heartbeat.
        poll_result = context.add_poll_result(120000)
        poll_result.add(TYPE_ACL_REQ,
                        {'type': "HEARTBEAT", 'rc': "SUCCESS"})
        agent.run()

        acl_req = context.sent_data[TYPE_ACL_REQ].pop()
        self.assertEqual(acl_req['type'], "GETACLSTATE")
        self.assertEqual(acl_req['endpoint_id'], endpoints[0].id)

        poll_result = context.add_poll_result(120000)
        poll_result.add(TYPE_ACL_REQ,
                        {'type': "GETACLSTATE", 'rc': "SUCCESS", 'message': "" })
        agent.run()

        acl_req = context.sent_data[TYPE_ACL_REQ].pop()
        self.assertEqual(acl_req['type'], "GETACLSTATE")
        self.assertEqual(acl_req['endpoint_id'], endpoints[1].id)

        #*********************************************************************#
        #* OK, so let's pretend that the ACL SUB connection has gone away.   *#
        #* We've done keepalives to the Nth degree, so are about to start    *#
        #* cheating a bit, and will tweak _last_activity in the socket to    *#
        #* force timeouts to make the test (much) simpler.                   *#
        #*********************************************************************#
        agent.sockets[TYPE_ACL_SUB]._last_activity = 0
        poll_result = context.add_poll_result(120000)
        agent.run()

        for sock in agent.sockets.values():
            if sock.type == TYPE_ACL_SUB:
                self.assertIsNot(sock_zmq[sock], sock._zmq)
                sock_zmq[sock] = sock._zmq
            else:
                self.assertIs(sock_zmq[sock], sock._zmq)

        #*********************************************************************#
        #* The ACL request connection is up, and it should send us 5         *#
        #* messages. Recall that there is already one outstanding            *#
        #* GETACLSTATE, so acknowledge that first.                           *#
        #*********************************************************************#
        for i in range(1,6):
            poll_result = context.add_poll_result(120000)
            poll_result.add(TYPE_ACL_REQ,
                            {'type': "GETACLSTATE", 'rc': "SUCCESS", 'message': ""})

            agent.run()

            acl_req = context.sent_data[TYPE_ACL_REQ].pop()
            self.assertEqual(acl_req['type'], "GETACLSTATE")


        #*********************************************************************#
        #* There should be no more messages - i.e. just the 5.               *#
        #*********************************************************************#
        poll_result = context.add_poll_result(120000)
        agent.run()
        self.assertFalse(context.sent_data_present())

        #*********************************************************************#
        #* Make the ACL REQ connection go away, with similar results.        *#
        #*********************************************************************#
        agent.sockets[TYPE_ACL_REQ]._last_activity = 0
        poll_result = context.add_poll_result(120000)
        agent.run()

        for sock in agent.sockets.values():
            if sock.type == TYPE_ACL_REQ:
                self.assertIsNot(sock_zmq[sock], sock._zmq)
                sock_zmq[sock] = sock._zmq
            else:
                self.assertIs(sock_zmq[sock], sock._zmq)

        for i in range(1,6):
            acl_req = context.sent_data[TYPE_ACL_REQ].pop()
            self.assertEqual(acl_req['type'], "GETACLSTATE")

            poll_result = context.add_poll_result(120000)
            poll_result.add(TYPE_ACL_REQ,
                            {'type': "GETACLSTATE", 'rc': "SUCCESS", 'message': ""})

            agent.run()      

    def test_queues(self):
        """
        Test queuing.
        """
        common.default_logging()
        context = stub_zmq.Context()
        agent = felix.FelixAgent(config_path, context)

        agent.config.RESYNC_INT_SEC = 500
        agent.config.CONN_TIMEOUT_MS = 50000
        agent.config.CONN_KEEPALIVE_MS = 5000

        # Get started.
        context.add_poll_result(0)
        agent.run()

        # Who cares about the resync request - just reply right away.
        resync_req = context.sent_data[TYPE_EP_REQ].pop()
        self.assertFalse(context.sent_data_present())

        context.add_poll_result(0)
        agent.run()
        resync_rsp = { 'type': "RESYNCSTATE",
                       'endpoint_count': "0",
                       'rc': "SUCCESS",
                       'message': "hello" }

        poll_result = context.add_poll_result(0)
        poll_result.add(TYPE_EP_REQ, resync_rsp)
        agent.run()

        # OK, so let's just trigger a bunch of endpoint creations. Each of
        # these does some work that we don't care about. What we do care about
        # is that the queues get managed.
        addrs = [ "192.168.0." + str(i) for i in range(1,11) ]
        endpoints = []
        for addr in addrs:
            endpoint = CreatedEndpoint([addr])
            endpoints.append(endpoint)

            poll_result = context.add_poll_result(1)
            poll_result.add(TYPE_EP_REP, endpoint.create_req)
            agent.run()

            endpoint_created_rsp = context.sent_data[TYPE_EP_REP].pop()
            self.assertEqual(endpoint_created_rsp['rc'], "SUCCESS")

        #*********************************************************************#
        #* OK, we just threw 10 ENDPOINTCREATED requests in. There should be *#
        #* 10 ACLUPDATE requests out there for those endpoints, in           *#
        #* order. Grab them, spinning things out long enough that keepalives *#
        #* would be sent and connections torn down if there was no other     *#
        #* activity.                                                         *#
        #*********************************************************************#
        sock_zmq = {}
        for sock in agent.sockets.values():
            sock_zmq[sock] = sock._zmq

        poll_result = context.add_poll_result(6000)
        poll_result.add(TYPE_EP_REP, {'type': "HEARTBEAT"})
        poll_result.add(TYPE_ACL_SUB, {'type': "HEARTBEAT"}, 'aclheartbeat')

        acl_req_sock = agent.sockets[TYPE_ACL_REQ]

        for i in range(1,11):
            log.debug("Check status; iteration %d", i)

            agent.run()

            self.assertEqual(len(acl_req_sock._send_queue), 10 - i)

            poll_result = context.add_poll_result(20000 * i)

            acl_req = context.sent_data[TYPE_ACL_REQ].pop()
            self.assertEqual(acl_req['type'], "GETACLSTATE")
            self.assertEqual(acl_req['endpoint_id'], endpoints[i - 1].id)
            poll_result.add(TYPE_ACL_REQ,
                            {'type': "GETACLSTATE", 'rc': "SUCCESS", 'message': "" })
 
            # Heartbeats for the other connections.
            keepalive_rsp = context.sent_data[TYPE_EP_REP].pop()
            self.assertEqual(keepalive_rsp['type'], "HEARTBEAT")
            poll_result.add(TYPE_EP_REP, {'type': "HEARTBEAT"})

            keepalive = context.sent_data[TYPE_EP_REQ].pop()
            self.assertEqual(keepalive['type'], "HEARTBEAT")
            poll_result.add(TYPE_EP_REQ, {'type': "HEARTBEAT", 'rc': "SUCCESS"})

            # ACL SUB does not need responses.
            poll_result.add(TYPE_ACL_SUB, {'type': "HEARTBEAT"}, 'aclheartbeat')

            self.assertFalse(context.sent_data_present())

            # We now wait long enough that a keepalive will appear.
            agent.run()
            poll_result = context.add_poll_result(20000 * i + 10000)

        # Check the ACL_REQ keepalives have started.
        poll_result = context.add_poll_result(20000 * i + 10000)
        agent.run()

        keepalive = context.sent_data[TYPE_ACL_REQ].pop()
        self.assertEqual(keepalive['type'], "HEARTBEAT")

        for sock in agent.sockets.values():
            # Assert no connections have been restarted.
            self.assertIs(sock_zmq[sock], sock._zmq)

    def test_resync_timeouts(self):
        """
        Test timeouts during resyncs
        """
        common.default_logging()
        context = stub_zmq.Context()
        stub_utils.set_time(100000)
        agent = felix.FelixAgent(config_path, context)

        agent.config.RESYNC_INT_SEC = 500
        agent.config.CONN_TIMEOUT_MS = 50000
        agent.config.CONN_KEEPALIVE_MS = 5000

        sock_zmq = {}
        for sock in agent.sockets.values():
            sock_zmq[sock] = sock._zmq

        # Get started.
        context.add_poll_result(100000)
        agent.run()

        # Check resync is there, throw it away.
        resync_req = context.sent_data[TYPE_EP_REQ].pop()
        self.assertEqual(resync_req['type'], "RESYNCSTATE")
        resync_id = resync_req['resync_id']
        self.assertFalse(context.sent_data_present())

        # Force resync to be replaced by tearing down EP_REQ when outstanding.
        agent.sockets[TYPE_EP_REQ]._last_activity = 0
        context.add_poll_result(100000)
        agent.run()

        for sock in agent.sockets.values():
            log.debug("Check socket %s", sock.type)
            if sock.type == TYPE_EP_REQ:
                self.assertIsNot(sock_zmq[sock], sock._zmq)
                sock_zmq[sock] = sock._zmq
            else:
                self.assertIs(sock_zmq[sock], sock._zmq)

        # As if by magic, another resync has appeared.
        resync_req = context.sent_data[TYPE_EP_REQ].pop()
        self.assertEqual(resync_req['type'], "RESYNCSTATE")
        self.assertNotEqual(resync_req['resync_id'], resync_id)
        resync_id = resync_req['resync_id']
        self.assertFalse(context.sent_data_present())

        resync_id = resync_req['resync_id']
        resync_rsp = { 'type': "RESYNCSTATE",
                       'endpoint_count': 0,
                       'rc': "SUCCESS",
                       'message': "hello" }

        poll_result = context.add_poll_result(100000)
        poll_result.add(TYPE_EP_REQ, resync_rsp)
        agent.run()

        # Force another resync by timing out the EP_REP connection.
        agent.sockets[TYPE_EP_REP]._last_activity = 0
        context.add_poll_result(100000)
        agent.run()

        for sock in agent.sockets.values():
            self.assertIs(sock_zmq[sock], sock._zmq)

        # As if by magic, another resync has appeared.
        resync_req = context.sent_data[TYPE_EP_REQ].pop()
        self.assertEqual(resync_req['type'], "RESYNCSTATE")
        self.assertNotEqual(resync_req['resync_id'], resync_id)
        resync_id = resync_req['resync_id']
        self.assertFalse(context.sent_data_present())


def get_blank_acls():
    """
    Return a blank set of ACLs, with nothing permitted.
    """
    acls = {}
    acls['v4'] = {}
    acls['v6'] = {}

    acls['v4']['inbound_default'] = "deny"
    acls['v4']['outbound_default'] = "deny"
    acls['v4']['inbound'] = []
    acls['v4']['outbound'] = []
    acls['v6']['inbound_default'] = "deny"
    acls['v6']['outbound_default'] = "deny"
    acls['v6']['inbound'] = []
    acls['v6']['outbound'] = []
    return acls

def set_expected_global_rules():
    """
    Sets up the minimal global rules we expect to have.
    """
    expected_iptables.reset()

    table = expected_iptables.tables_v4["filter"]
    stub_fiptables.get_chain(table, "felix-TO-ENDPOINT")
    stub_fiptables.get_chain(table, "felix-FROM-ENDPOINT")
    stub_fiptables.get_chain(table, "felix-FORWARD")
    stub_fiptables.get_chain(table, "felix-INPUT")
    chain = table._chains_dict["FORWARD"]
    chain.rules.append(stub_fiptables.Rule(IPV4, "felix-FORWARD"))
    chain = table._chains_dict["INPUT"]
    chain.rules.append(stub_fiptables.Rule(IPV4, "felix-INPUT"))

    chain = table._chains_dict["felix-FORWARD"]
    rule  = stub_fiptables.Rule(type, "felix-FROM-ENDPOINT")
    rule.in_interface = "tap+"
    chain.rules.append(rule)
    rule  = stub_fiptables.Rule(type, "felix-TO-ENDPOINT")
    rule.out_interface = "tap+"
    chain.rules.append(rule)
    rule  = stub_fiptables.Rule(type, "ACCEPT")
    rule.in_interface = "tap+"
    chain.rules.append(rule)
    rule  = stub_fiptables.Rule(type, "ACCEPT")
    rule.out_interface = "tap+"
    chain.rules.append(rule)

    chain = table._chains_dict["felix-INPUT"]
    rule  = stub_fiptables.Rule(type, "felix-FROM-ENDPOINT")
    rule.in_interface = "tap+"
    chain.rules.append(rule)
    rule  = stub_fiptables.Rule(type, "ACCEPT")
    rule.in_interface = "tap+"
    chain.rules.append(rule)

    table = expected_iptables.tables_v4["nat"]
    chain = table._chains_dict["PREROUTING"]
    chain.rules.append(stub_fiptables.Rule(IPV4, "felix-PREROUTING"))

    chain = stub_fiptables.get_chain(table, "felix-PREROUTING")
    rule = stub_fiptables.Rule(IPV4)
    rule.protocol = "tcp"
    rule.create_tcp_match("80")
    rule.create_target("DNAT", {'to_destination': '127.0.0.1:9697'})
    chain.rules.append(rule)

    table = expected_iptables.tables_v6["filter"]
    stub_fiptables.get_chain(table, "felix-TO-ENDPOINT")
    stub_fiptables.get_chain(table, "felix-FROM-ENDPOINT")
    stub_fiptables.get_chain(table, "felix-FORWARD")
    stub_fiptables.get_chain(table, "felix-INPUT")
    chain = table._chains_dict["FORWARD"]
    chain.rules.append(stub_fiptables.Rule(IPV6, "felix-FORWARD"))
    chain = table._chains_dict["INPUT"]
    chain.rules.append(stub_fiptables.Rule(IPV6, "felix-INPUT"))

    chain = table._chains_dict["felix-FORWARD"]
    rule  = stub_fiptables.Rule(type, "felix-FROM-ENDPOINT")
    rule.in_interface = "tap+"
    chain.rules.append(rule)
    rule  = stub_fiptables.Rule(type, "felix-TO-ENDPOINT")
    rule.out_interface = "tap+"
    chain.rules.append(rule)
    rule  = stub_fiptables.Rule(type, "ACCEPT")
    rule.in_interface = "tap+"
    chain.rules.append(rule)
    rule  = stub_fiptables.Rule(type, "ACCEPT")
    rule.out_interface = "tap+"
    chain.rules.append(rule)

    chain = table._chains_dict["felix-INPUT"]
    rule  = stub_fiptables.Rule(type, "felix-FROM-ENDPOINT")
    rule.in_interface = "tap+"
    chain.rules.append(rule)
    rule  = stub_fiptables.Rule(type, "ACCEPT")
    rule.in_interface = "tap+"
    chain.rules.append(rule)


def add_endpoint_rules(suffix, tap, ipv4, ipv6, mac):
    """
    This adds the rules for an endpoint, appending to the end. This generates
    a clean state to allow us to test that the state is correct, even after
    it starts with extra rules etc.
    """
    table = expected_iptables.tables_v4["filter"]
    chain = table._chains_dict["felix-FROM-ENDPOINT"]
    rule = stub_fiptables.Rule(IPV4, "felix-from-%s" % suffix)
    rule.in_interface = tap
    chain.rules.append(rule)

    chain = table._chains_dict["felix-TO-ENDPOINT"]
    rule = stub_fiptables.Rule(IPV4, "felix-to-%s" % suffix)
    rule.out_interface = tap
    chain.rules.append(rule)

    chain = stub_fiptables.get_chain(table, "felix-from-%s" % suffix)
    rule = stub_fiptables.Rule(IPV4, "DROP")
    rule.create_conntrack_match(["INVALID"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV4, "RETURN")
    rule.create_conntrack_match(["RELATED,ESTABLISHED"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV4, "RETURN")
    rule.protocol = "udp"
    rule.create_udp_match("68", "67")
    chain.rules.append(rule)

    if ipv4 is not None:
        rule = stub_fiptables.Rule(IPV4)
        rule.create_target("MARK", {"set_mark": "1"})
        rule.src = ipv4
        rule.create_mac_match(mac)
        chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV4, "DROP")
    rule.create_mark_match("!1")
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV4, "RETURN")
    rule.create_set_match(["felix-from-port-%s" % suffix, "dst,dst"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV4, "RETURN")
    rule.create_set_match(["felix-from-addr-%s" % suffix, "dst"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV4, "RETURN")
    rule.protocol = "icmp"
    rule.create_set_match(["felix-from-icmp-%s" % suffix, "dst"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV4, "DROP")
    chain.rules.append(rule)

    chain = stub_fiptables.get_chain(table, "felix-to-%s" % suffix)
    rule = stub_fiptables.Rule(IPV4, "DROP")
    rule.create_conntrack_match(["INVALID"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV4, "RETURN")
    rule.create_conntrack_match(["RELATED,ESTABLISHED"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV4, "RETURN")
    rule.create_set_match(["felix-to-port-%s" % suffix, "src,dst"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV4, "RETURN")
    rule.create_set_match(["felix-to-addr-%s" % suffix, "src"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV4, "RETURN")
    rule.protocol = "icmp"
    rule.create_set_match(["felix-to-icmp-%s" % suffix, "src"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV4, "DROP")
    chain.rules.append(rule)

    table = expected_iptables.tables_v6["filter"]
    chain = table._chains_dict["felix-FROM-ENDPOINT"]
    rule = stub_fiptables.Rule(IPV6, "felix-from-%s" % suffix)
    rule.in_interface = tap
    chain.rules.append(rule)

    chain = table._chains_dict["felix-TO-ENDPOINT"]
    rule = stub_fiptables.Rule(IPV6, "felix-to-%s" % suffix)
    rule.out_interface = tap
    chain.rules.append(rule)

    chain = stub_fiptables.get_chain(table, "felix-from-%s" % suffix)
    rule = stub_fiptables.Rule(type, "RETURN")
    rule.protocol = "icmpv6"
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV6, "DROP")
    rule.create_conntrack_match(["INVALID"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV6, "RETURN")
    rule.create_conntrack_match(["RELATED,ESTABLISHED"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV6, "RETURN")
    rule.protocol = "udp"
    rule.create_udp_match("546", "547")
    chain.rules.append(rule)

    if ipv6 is not None:
        rule = stub_fiptables.Rule(IPV6)
        rule.create_target("MARK", {"set_mark": "1"})
        rule.src = ipv6
        rule.create_mac_match(mac)
        chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV6, "DROP")
    rule.create_mark_match("!1")
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV6, "RETURN")
    rule.create_set_match(["felix-6-from-port-%s" % suffix, "dst,dst"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV6, "RETURN")
    rule.create_set_match(["felix-6-from-addr-%s" % suffix, "dst"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV6, "RETURN")
    rule.protocol = "icmpv6"
    rule.create_set_match(["felix-6-from-icmp-%s" % suffix, "dst"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV6, "DROP")
    chain.rules.append(rule)

    chain = stub_fiptables.get_chain(table, "felix-to-%s" % suffix)
    for icmp in ["130", "131", "132", "134", "135", "136"]:
        rule = stub_fiptables.Rule(futils.IPV6, "RETURN")
        rule.protocol = "icmpv6"
        rule.create_icmp6_match([icmp])
        chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV6, "DROP")
    rule.create_conntrack_match(["INVALID"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV6, "RETURN")
    rule.create_conntrack_match(["RELATED,ESTABLISHED"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV6, "RETURN")
    rule.create_set_match(["felix-6-to-port-%s" % suffix, "src,dst"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV6, "RETURN")
    rule.create_set_match(["felix-6-to-addr-%s" % suffix, "src"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV6, "RETURN")
    rule.protocol = "icmpv6"
    rule.create_set_match(["felix-6-to-icmp-%s" % suffix, "src"])
    chain.rules.append(rule)

    rule = stub_fiptables.Rule(IPV6, "DROP")
    chain.rules.append(rule)


def add_endpoint_ipsets(suffix):
    """
    Sets up the ipsets for a given endpoint. Actual entries in these endpoints
    must then be added manually.
    """
    # Create ipsets if they do not already exist.
    expected_ipsets.create("felix-to-port-" + suffix, "hash:net,port", "inet")
    expected_ipsets.create("felix-to-addr-" + suffix, "hash:net", "inet")
    expected_ipsets.create("felix-to-icmp-" + suffix, "hash:net", "inet")
    expected_ipsets.create("felix-from-port-" + suffix, "hash:net,port", "inet")
    expected_ipsets.create("felix-from-addr-" + suffix, "hash:net", "inet")
    expected_ipsets.create("felix-from-icmp-" + suffix, "hash:net", "inet")

    expected_ipsets.create("felix-6-to-port-" + suffix, "hash:net,port", "inet6")
    expected_ipsets.create("felix-6-to-addr-" + suffix, "hash:net", "inet6")
    expected_ipsets.create("felix-6-to-icmp-" + suffix, "hash:net", "inet6")
    expected_ipsets.create("felix-6-from-port-" + suffix, "hash:net,port", "inet6")
    expected_ipsets.create("felix-6-from-addr-" + suffix, "hash:net", "inet6")
    expected_ipsets.create("felix-6-from-icmp-" + suffix, "hash:net", "inet6")

class CreatedEndpoint(object):
    """
    Builds an object which contains all the information we might need. Useful
    if we want to just create one for test purposes.

    addresses is a list or set of addresses; we just need to iterate over it.
    """
    def __init__(self, addresses, resync_id=""):
        self.id = str(uuid.uuid4())
        self.mac = stub_utils.get_mac()
        self.suffix = self.id[:11]
        self.tap = "tap" + self.suffix
        addrs = []
        for addr in addresses:
            if "." in addr:
                addrs.append({'gateway': "1.2.3.1", 'addr': addr})
            else:
                addrs.append({'gateway': "2001::1234", 'addr': addr})
                
        self.create_req = { 'type': "ENDPOINTCREATED",
                     'endpoint_id': self.id,
                     'resync_id': resync_id,
                     'issued': str(futils.time_ms()),
                     'mac': self.mac,
                     'state': Endpoint.STATE_ENABLED,
                     'addrs': addrs }

        self.destroy_req = { 'type': "ENDPOINTDESTROYED",
                             'endpoint_id': self.id,
                             'issued': futils.time_ms() }

        tap_obj = stub_devices.TapInterface(self.tap)
        stub_devices.add_tap(tap_obj)
