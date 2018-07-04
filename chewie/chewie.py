from eventlet import sleep, GreenPool
from eventlet.green import socket
from eventlet.queue import Queue
from fcntl import ioctl

import struct

from chewie.eap_state_machine import FullEAPStateMachine
from chewie.radius_attributes import EAPMessage, State
from chewie.state_machine import StateMachine

from .message_parser import MessageParser, MessagePacker
from .mac_address import MacAddress
from .event import EventMessageReceived, EventRadiusMessageReceived


def unpack_byte_string(byte_string):
    return "".join("%02x" % x for x in byte_string)


class Chewie(object):
    SIOCGIFHWADDR = 0x8927
    SIOCGIFINDEX = 0x8933
    PACKET_MR_MULTICAST = 0
    PACKET_MR_PROMISC = 1
    SOL_PACKET = 263
    PACKET_ADD_MEMBERSHIP = 1
    EAP_ADDRESS = MacAddress.from_string("01:80:c2:00:00:03")
    RADIUS_UDP_PORT = 1812

    def __init__(self, interface_name, credentials, logger=None, auth_handler=None, group_address=None, radius_ip=None):
        self.interface_name = interface_name
        self.credentials = credentials
        self.logger = logger
        self.auth_handler = auth_handler
        self.group_address = group_address
        if not group_address:
            self.group_address = self.EAP_ADDRESS

        self.radius_ip = radius_ip
        self.radius_secret = "SECRET"
        self.radius_listen_port = 0

        self.state_machines = {}  # mac: sm
        self.packet_id_to_mac = {}  # radius_packet_id: mac

        self.eap_output_messages = Queue()
        self.radius_output_messages = Queue()

        self.radius_id = -1

    def run(self):
        self.logger.info("Starting")
        self.open_socket()
        self.open_radius_socket()
        self.get_interface_info()
        self.join_multicast_group()
        self.start_threads_and_wait()

    def start_threads_and_wait(self):
        self.pool = GreenPool()
        self.eventlets = []

        self.eventlets.append(self.pool.spawn(self.send_eap_messages))
        self.eventlets.append(self.pool.spawn(self.receive_eap_messages))

        self.eventlets.append(self.pool.spawn(self.send_radius_messages))
        self.eventlets.append(self.pool.spawn(self.receive_radius_messages))
        self.pool.waitall()

    def auth_success(self, src_mac):
        if self.auth_handler:
            self.auth_handler(src_mac, self.group_address)

    def send_eap_messages(self):
        try:
            while True:
                sleep(0)
                message, src_mac = self.eap_output_messages.get()
                self.logger.info("Sending message %s to %s" % (message, str(self.group_address)))
                self.socket.send(MessagePacker.ethernet_pack(message, src_mac, self.group_address))
        except Exception as e:
            self.logger.exception(e)

    def receive_eap_messages(self):
        try:
            while True:
                sleep(0)
                self.logger.info("waiting for eap.")
                packed_message = self.socket.recv(4096)
                self.logger.info("Received packed_message: %s", str(packed_message))

                message = MessageParser.ethernet_parse(packed_message)
                self.logger.info("eap EAP(): %s", message)
                self.logger.info("Received message: %s" % message.__dict__)
                sm = self.get_state_machine(message.src_mac)
                event = EventMessageReceived(message)
                sm.event(event)
        except Exception as e:
            self.logger.exception(e)

    def send_radius_messages(self):
        try:
            while True:
                sleep(0)
                eap_message, src_mac, username, state = self.radius_output_messages.get()
                self.logger.info("got radius to send.. mac: %s %s, username: %s", type(src_mac), src_mac, username)
                radius_packet_id = self.get_next_radius_packet_id()
                self.packet_id_to_mac[radius_packet_id] = src_mac
                # message is eap. needs to be wrapped into a radius packet.
                self.logger.info("Sending RADIUS message %s with state %s", eap_message, state)
                data = MessagePacker.radius_pack(eap_message, src_mac, username,
                                                 radius_packet_id, state, self.radius_secret)
                self.logger.warning("sending data: %s", data)
                self.radius_socket.sendto(data, (self.radius_ip, self.RADIUS_UDP_PORT))
                self.logger.info("sent radius message.")
        except Exception as e:
            self.logger.exception(e)

    def receive_radius_messages(self):
        try:
            while True:
                sleep(0)
                self.logger.info("waiting for radius.")
                packed_message = self.radius_socket.recv(4096)
                self.logger.info("got radius. parsing....")
                radius = MessageParser.radius_parse(packed_message)
                self.logger.info("Received RADIUS message: %s", radius)
                eap_msg = radius.attributes.find(EAPMessage.DESCRIPTION)
                state = radius.attributes.find(State.DESCRIPTION)
                self.logger.info("radius EAP(%d): %s", len(eap_msg.data_type.data), eap_msg.data_type.data)
                event = EventRadiusMessageReceived(eap_msg, state)
                # RADIUS Events can be Access-Accept, Access-Reject, Access-Challenge.
                # For each event send the eap-message back to the supplicant.
                # And maybe do something with it locally (accept-faucet acl)
                sm = self.get_state_machine_from_radius_packet_id(radius.packet_id)
                self.logger.warning('sm %s from packet_id %d', str(sm), radius.packet_id)
                self.logger.warning('ev.msg %s', event.message)
                sm.event(event)
        except Exception as e:
            self.logger.exception(e)

    def open_radius_socket(self):
        self.radius_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.logger.info("%s %d" % ("127.0.0.1", self.radius_listen_port))
        self.radius_socket.bind(("", self.radius_listen_port))

    def open_socket(self):
        self.socket = socket.socket(socket.PF_PACKET, socket.SOCK_RAW, socket.htons(0x888e))
        self.socket.bind((self.interface_name, 0))

    def build_state_machine(self):
        self.state_machine = StateMachine(self.interface_address, self.auth_success)

    def get_interface_info(self):
        self.get_interface_address()
        self.get_interface_index()

    def get_interface_address(self):
        # http://man7.org/linux/man-pages/man7/netdevice.7.html
        ifreq = struct.pack('16sH6s', self.interface_name.encode("utf-8"), 0, b"")
        response = ioctl(self.socket, self.SIOCGIFHWADDR, ifreq)
        _interface_name, _address_family, interface_address = struct.unpack('16sH6s', response)
        self.interface_address = MacAddress(interface_address)

    def get_interface_index(self):
        # http://man7.org/linux/man-pages/man7/netdevice.7.html
        ifreq = struct.pack('16sI', self.interface_name.encode("utf-8"), 0)
        response = ioctl(self.socket, self.SIOCGIFINDEX, ifreq)
        _ifname, self.interface_index = struct.unpack('16sI', response)

    def join_multicast_group(self):
        # TODO this works but should blank out the end bytes
        mreq = struct.pack("IHH8s", self.interface_index, self.PACKET_MR_PROMISC,
                           len(self.EAP_ADDRESS.address), self.EAP_ADDRESS.address)
        self.socket.setsockopt(self.SOL_PACKET, self.PACKET_ADD_MEMBERSHIP, mreq)

    def get_state_machine_from_radius_packet_id(self, packet_id):
        return self.get_state_machine(self.packet_id_to_mac[packet_id])

    def get_state_machine(self, src_mac):
        sm = self.state_machines.get(src_mac, None)
        if not sm:
            sm = FullEAPStateMachine(self.eap_output_messages, self.radius_output_messages, src_mac)
            sm.eapRestart = True
            # TODO what if port is not actually enabled, but then how did they auth?
            sm.portEnabled = True
            self.state_machines[src_mac] = sm
        return sm

    def get_next_radius_packet_id(self):
        self.radius_id += 1
        if self.radius_id > 255:
            self.radius_id = 0
        return self.radius_id
