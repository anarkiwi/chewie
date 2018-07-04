import os

from chewie.radius import RadiusAttributesList, RadiusAccessRequest, Radius
from chewie.radius_attributes import FramedMTU, CallingStationId, ServiceType, NASPortType, CalledStationId, UserName, \
    MessageAuthenticator, EAPMessage
from chewie.radius_datatypes import Text
from .ethernet_packet import EthernetPacket
from .auth_8021x import Auth8021x
from .eap import Eap, EapIdentity, EapMd5Challenge, EapSuccess, EapFailure, EapLegacyNak, EapTTLS


class SuccessMessage(object):
    def __init__(self, src_mac, message_id):
        self.src_mac = src_mac
        self.message_id = message_id

    @classmethod
    def build(cls, src_mac, eap):
        return cls(src_mac, eap.packet_id)


class FailureMessage(object):
    def __init__(self, src_mac, message_id):
        self.src_mac = src_mac
        self.message_id = message_id

    @classmethod
    def build(cls, src_mac, eap):
        return cls(src_mac, eap.packet_id)


class IdentityMessage(object):
    def __init__(self, src_mac, message_id, code, identity):
        self.src_mac = src_mac
        self.message_id = message_id
        self.code = code
        self.identity = identity

    @classmethod
    def build(cls, src_mac, eap):
        return cls(src_mac, eap.packet_id, eap.code, eap.identity)


class LegacyNakMessage(object):
    def __init__(self, src_mac, message_id, code, desired_auth_types):
        self.src_mac = src_mac
        self.message_id = message_id
        self.code = code
        self.desired_auth_types = desired_auth_types

    @classmethod
    def build(cls, src_mac, eap):
        return cls(src_mac, eap.packet_id, eap.code, eap.desired_auth_types)


class Md5ChallengeMessage(object):
    def __init__(self, src_mac, message_id, code, challenge, extra_data):
        self.src_mac = src_mac
        self.message_id = message_id
        self.code = code
        self.challenge = challenge
        self.extra_data = extra_data

    @classmethod
    def build(cls, src_mac, eap):
        return cls(src_mac, eap.packet_id, eap.code, eap.challenge, eap.extra_data)


class TtlsMessage(object):
    def __init__(self, src_mac, message_id, code, flags, extra_data):
        self.src_mac = src_mac
        self.message_id = message_id
        self.code = code
        self.flags = flags
        self.extra_data = extra_data

    @classmethod
    def build(cls, src_mac, eap):
        return cls(src_mac, eap.packet_id, eap.code, eap.flags, eap.extra_data)


class EapolStartMessage(object):
    def __init__(self, src_mac):
        self.src_mac = src_mac

    @classmethod
    def build(cls, src_mac):
        return cls(src_mac)


class EapolLogoffMessage(object):
    def __init__(self, src_mac):
        self.src_mac = src_mac

    @classmethod
    def build(cls, src_mac):
        return cls(src_mac)


EAP_MESSAGES = {
    Eap.IDENTITY: IdentityMessage,
    Eap.MD5_CHALLENGE: Md5ChallengeMessage,
    Eap.LEGACY_NAK: LegacyNakMessage,
    Eap.TTLS: TtlsMessage,
}

AUTH_8021X_MESSAGES = {
    0: "eap",
    1: "eapol start",
}


class MessageParser:
    @staticmethod
    def eap_parse(data, src_mac):
        auth_8021x = Auth8021x.parse(data)
        if auth_8021x.packet_type == 0:
            eap = Eap.parse(auth_8021x.data)
            if isinstance(eap, EapIdentity) or isinstance(eap, EapMd5Challenge) \
                    or isinstance(eap, EapLegacyNak) or isinstance(eap, EapTTLS):
                return EAP_MESSAGES[eap.PACKET_TYPE].build(src_mac, eap)
            elif isinstance(eap, EapSuccess):
                return SuccessMessage.build(src_mac, eap)
            elif isinstance(eap, EapFailure):
                return FailureMessage.build(src_mac, eap)
            else:
                raise ValueError("Got bad Eap packet: %s" % eap)
        elif auth_8021x.packet_type == 1:
            return EapolStartMessage.build(src_mac)
        elif auth_8021x.packet_type == 2:
            return EapolLogoffMessage.build(src_mac)
        raise ValueError("802.1x has bad type, expected 0: %s" % auth_8021x)

    @staticmethod
    def ethernet_parse(packed_message):
        ethernet_packet = EthernetPacket.parse(packed_message)
        if ethernet_packet.ethertype != 0x888e:
            raise ValueError("Ethernet packet with bad ethertype received: %s" % ethernet_packet)

        return MessageParser.eap_parse(ethernet_packet.data, ethernet_packet.src_mac)

    @staticmethod
    def radius_parse(packed_message):
        parsed_radius = Radius.parse(packed_message)
        return parsed_radius


class EapMessage(object):
    pass


class MessagePacker:
    @staticmethod
    def ethernet_pack(message, src_mac, dst_mac):
        data = MessagePacker.pack(message)
        ethernet_packet = EthernetPacket(dst_mac=dst_mac, src_mac=src_mac, ethertype=0x888e, data=data)
        return ethernet_packet.pack()

    @staticmethod
    def radius_pack(eap_message, src_mac, username, radius_packet_id, state, secret):
        _, _, packed_eap = MessagePacker.eap_pack(eap_message)
        attr_list = list()
        attr_list.append(UserName.parse(username.encode()))
        attr_list.append(CalledStationId.parse("44-44-44-44-44-44:".encode()))
        attr_list.append(NASPortType.parse(bytes.fromhex("00000013")))
        attr_list.append(ServiceType.parse(bytes.fromhex("00000002")))
        attr_list.append(CallingStationId(Text(str(src_mac.address))))
        attr_list.append(FramedMTU.parse(bytes.fromhex("00000578")))

        # TODO could this not be just eap_message
        attr_list.append(EAPMessage.parse(packed_eap))

        if state:
            attr_list.append(state)

        attr_list.append(MessageAuthenticator.parse(bytes.fromhex("00000000000000000000000000000000")))

        request_authenticator = os.urandom(16)
        attributes = RadiusAttributesList(attr_list)
        access_request = RadiusAccessRequest(radius_packet_id, request_authenticator, attributes)
        return access_request.build(secret)

    @staticmethod
    def eap_pack(message):
        if isinstance(message, IdentityMessage) or isinstance(message, EapIdentity):
            if isinstance(message, EapIdentity):
                eap = message
            else:
                eap = EapIdentity(message.code, message.message_id, message.identity)
            version = 1
            packet_type = 0
            data = eap.pack()
        elif isinstance(message, LegacyNakMessage) or isinstance(message, EapLegacyNak):
            if isinstance(message, EapLegacyNak):
                eap = message
            else:
                eap = EapLegacyNak(message.code, message.message_id, message.desired_auth_types)
            version = 1
            packet_type = 0
            data = eap.pack()
        elif isinstance(message, Md5ChallengeMessage) or isinstance(message, EapMd5Challenge):
            if isinstance(message, EapMd5Challenge):
                eap = message
            else:
                eap = EapMd5Challenge(message.code, message.message_id, message.challenge, message.extra_data)
            version = 1
            packet_type = 0
            data = eap.pack()
        elif isinstance(message, TtlsMessage) or isinstance(message, EapTTLS):
            if isinstance(message, EapTTLS):
                eap = message
            else:
                eap = EapTTLS(message.code, message.message_id, message.flags, message.extra_data)
            version = 1
            packet_type = 0
            data = eap.pack()
        elif isinstance(message, SuccessMessage) or isinstance(message, EapSuccess):
            if isinstance(message, EapSuccess):
                eap = message
            else:
                eap = EapSuccess(message.message_id)
            version = 1
            packet_type = 0
            data = eap.pack()
        elif isinstance(message, FailureMessage) or isinstance(message, EapFailure):
            if isinstance(message, EapFailure):
                eap = message
            else:
                eap = EapFailure(message.message_id)
            version = 1
            packet_type = 0
            data = eap.pack()
        elif isinstance(message, EapolStartMessage):
            version = 1
            packet_type = 1
            data = b""
        elif isinstance(message, EapolLogoffMessage):
            version = 1
            packet_type = 2
            data = b""
        else:
            raise ValueError("Cannot pack message: %s" % message)
        return version, packet_type, data

    @staticmethod
    def pack(message):
        version, packet_type, data = MessagePacker.eap_pack(message)
        auth_8021x = Auth8021x(version=version, packet_type=packet_type, data=data)
        return auth_8021x.pack()
