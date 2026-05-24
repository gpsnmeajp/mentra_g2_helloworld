from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime
import inspect
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData


LOGGER = logging.getLogger("even_g2")


# ===== BEGIN adapted from Mentra-Community/MentraOS/blob/dev/mobile/modules/bluetooth-sdk/android/src/main/java/com/mentra/bluetoothsdk/sgcs/G2.kt =====
# Protocol constants, protobuf helpers, message builders, and BLE packet
# framing in this block are Python adaptations of the local Kotlin reference.
class G2BLE:
    WRITE_UUID = "00002760-08c2-11e1-9073-0e8ac72e5401"
    NOTIFY_UUID = "00002760-08c2-11e1-9073-0e8ac72e5402"
    AUDIO_NOTIFY_UUID = "00002760-08c2-11e1-9073-0e8ac72e6402"
    SERVICE_UUID = "00002760-08c2-11e1-9073-0e8ac72e0000"

    HEADER_BYTE = 0xAA
    SOURCE_PHONE = 0x01
    DEST_GLASSES = 0x02
    MAX_PACKET_PAYLOAD = 236


class ServiceID(IntEnum):
    DASHBOARD = 0x01
    MENU = 0x03
    EVEN_AI = 0x07
    G2_SETTING = 0x09
    GESTURE_CTRL = 0x0D
    ONBOARDING = 0x10
    DEVICE_SETTINGS = 0x80
    EVEN_HUB_CTRL = 0x81
    EVEN_HUB = 0xE0


class EvenHubCmd(IntEnum):
    CREATE_STARTUP_PAGE = 0
    UPDATE_IMAGE_RAW_DATA = 3
    UPDATE_TEXT_DATA = 5
    REBUILD_PAGE = 7
    SHUTDOWN_PAGE = 9
    HEARTBEAT = 12
    AUDIO_CONTROL = 15


class DevCfgCommandId(IntEnum):
    AUTHENTICATION = 4
    PIPE_ROLE_CHANGE = 5
    RING_CONNECT_INFO = 6
    BASE_CONN_HEART_BEAT = 14
    TIME_SYNC = 128


WRITE_GAP_SECONDS = 0.008
AUTH_STEP_DELAY_SECONDS = 0.2
INIT_COMMAND_GAP_SECONDS = 0.05
PAIR_SETTLE_SECONDS = 0.5
CONNECT_TIMEOUT_SECONDS = 60.0
BLEAKCLIENT_ACCEPTS_PAIR = "pair" in inspect.signature(BleakClient).parameters


def calc_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc = ((crc >> 8) | ((crc << 8) & 0xFF00)) ^ byte
        crc ^= (crc & 0xFF) >> 4
        crc ^= (crc << 12) & 0xFFFF
        crc ^= ((crc & 0xFF) << 5) & 0xFFFF
    return crc & 0xFFFF


class ProtobufWriter:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def write_varint(self, value: int) -> None:
        if value < 0:
            value += 1 << 64
        while value > 0x7F:
            self._buffer.append((value & 0x7F) | 0x80)
            value >>= 7
        self._buffer.append(value & 0x7F)

    def write_int32_field(self, field_number: int, value: int) -> None:
        self.write_varint(field_number << 3)
        self.write_varint(value)

    def write_string_field(self, field_number: int, value: str) -> None:
        encoded = value.encode("utf-8")
        self.write_bytes_field(field_number, encoded)

    def write_bytes_field(self, field_number: int, value: bytes) -> None:
        self.write_varint((field_number << 3) | 2)
        self.write_varint(len(value))
        self._buffer.extend(value)

    def write_message_field(self, field_number: int, sub_message: bytes) -> None:
        self.write_bytes_field(field_number, sub_message)

    def write_bool_field(self, field_number: int, value: bool) -> None:
        self.write_int32_field(field_number, 1 if value else 0)

    def to_bytes(self) -> bytes:
        return bytes(self._buffer)


class ProtobufReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def _read_varint(self) -> int | None:
        result = 0
        shift = 0
        while self._offset < len(self._data):
            byte = self._data[self._offset]
            self._offset += 1
            result |= (byte & 0x7F) << shift
            if byte & 0x80 == 0:
                return result
            shift += 7
            if shift > 63:
                return None
        return None

    def _read_tag(self) -> tuple[int, int] | None:
        tag = self._read_varint()
        if tag is None:
            return None
        return tag >> 3, tag & 0x07

    def _read_bytes(self) -> bytes | None:
        length = self._read_varint()
        if length is None:
            return None
        end = self._offset + length
        if end > len(self._data):
            return None
        value = self._data[self._offset:end]
        self._offset = end
        return value

    def _skip_field(self, wire_type: int) -> None:
        if wire_type == 0:
            self._read_varint()
        elif wire_type == 1:
            self._offset += 8
        elif wire_type == 2:
            self._read_bytes()
        elif wire_type == 5:
            self._offset += 4

    def parse_fields(self) -> dict[int, int | bytes]:
        fields: dict[int, int | bytes] = {}
        while self._offset < len(self._data):
            tag = self._read_tag()
            if tag is None:
                break
            field_number, wire_type = tag
            if wire_type == 0:
                value = self._read_varint()
                if value is not None:
                    fields[field_number] = value
            elif wire_type == 2:
                value = self._read_bytes()
                if value is not None:
                    fields[field_number] = value
            else:
                self._skip_field(wire_type)
        return fields


class EvenHubProto:
    @staticmethod
    def text_container_property(
        *,
        x: int,
        y: int,
        width: int,
        height: int,
        border_width: int = 0,
        border_color: int = 0,
        border_radius: int = 0,
        padding_length: int = 0,
        container_id: int,
        container_name: str | None = None,
        is_event_capture: bool = False,
        content: str | None = None,
    ) -> bytes:
        writer = ProtobufWriter()
        writer.write_int32_field(1, x)
        writer.write_int32_field(2, y)
        writer.write_int32_field(3, width)
        writer.write_int32_field(4, height)
        writer.write_int32_field(5, border_width)
        writer.write_int32_field(6, border_color)
        writer.write_int32_field(7, border_radius)
        writer.write_int32_field(8, padding_length)
        writer.write_int32_field(9, container_id)
        if container_name:
            writer.write_string_field(10, container_name)
        writer.write_int32_field(11, 1 if is_event_capture else 0)
        if content is not None:
            writer.write_string_field(12, content)
        return writer.to_bytes()

    @staticmethod
    def create_startup_page_container(
        *, text_containers: list[bytes] | None = None, image_containers: list[bytes] | None = None
    ) -> bytes:
        text_containers = text_containers or []
        image_containers = image_containers or []
        writer = ProtobufWriter()
        writer.write_int32_field(1, len(text_containers) + len(image_containers))
        for item in text_containers:
            writer.write_message_field(3, item)
        for item in image_containers:
            writer.write_message_field(4, item)
        return writer.to_bytes()

    @staticmethod
    def even_hub_message(
        cmd: EvenHubCmd,
        sub_field_number: int,
        sub_message: bytes,
        *,
        magic_random: int = 0,
        app_id: int | None = None,
    ) -> bytes:
        writer = ProtobufWriter()
        writer.write_int32_field(1, int(cmd))
        writer.write_int32_field(2, magic_random)
        writer.write_message_field(sub_field_number, sub_message)
        if app_id is not None:
            writer.write_int32_field(5, app_id)
        return writer.to_bytes()

    @classmethod
    def create_page_message(
        cls,
        *,
        text_containers: list[bytes] | None = None,
        image_containers: list[bytes] | None = None,
        magic_random: int = 0,
    ) -> bytes:
        container = cls.create_startup_page_container(
            text_containers=text_containers,
            image_containers=image_containers,
        )
        return cls.even_hub_message(
            EvenHubCmd.CREATE_STARTUP_PAGE,
            3,
            container,
            magic_random=magic_random,
        )

    @classmethod
    def update_text_message(
        cls,
        *,
        container_id: int,
        content_offset: int,
        content_length: int,
        content: str,
    ) -> bytes:
        writer = ProtobufWriter()
        writer.write_int32_field(1, container_id)
        writer.write_int32_field(3, content_offset)
        writer.write_int32_field(4, content_length)
        writer.write_string_field(5, content)
        return cls.even_hub_message(EvenHubCmd.UPDATE_TEXT_DATA, 9, writer.to_bytes())

    @classmethod
    def heartbeat_message(cls, *, magic_random: int = 0) -> bytes:
        heartbeat = ProtobufWriter().to_bytes()
        return cls.even_hub_message(
            EvenHubCmd.HEARTBEAT,
            14,
            heartbeat,
            magic_random=magic_random,
        )


class DevSettingsProto:
    @staticmethod
    def auth_cmd(magic_random: int) -> bytes:
        writer = ProtobufWriter()
        writer.write_int32_field(1, DevCfgCommandId.AUTHENTICATION)
        writer.write_int32_field(2, magic_random)

        auth_writer = ProtobufWriter()
        auth_writer.write_bool_field(1, True)
        auth_writer.write_int32_field(2, 4)

        writer.write_message_field(3, auth_writer.to_bytes())
        return writer.to_bytes()

    @staticmethod
    def pipe_role_change(magic_random: int) -> bytes:
        writer = ProtobufWriter()
        writer.write_int32_field(1, DevCfgCommandId.PIPE_ROLE_CHANGE)
        writer.write_int32_field(2, magic_random)

        role_writer = ProtobufWriter()
        role_writer.write_int32_field(1, 1)

        writer.write_message_field(4, role_writer.to_bytes())
        return writer.to_bytes()

    @staticmethod
    def time_sync(magic_random: int) -> bytes:
        writer = ProtobufWriter()
        writer.write_int32_field(1, DevCfgCommandId.TIME_SYNC)
        writer.write_int32_field(2, magic_random)

        sync_writer = ProtobufWriter()
        sync_writer.write_int32_field(1, int(time.time()))
        offset = datetime.now().astimezone().utcoffset()
        timezone_hours = int(offset.total_seconds() / 3600) if offset is not None else 0
        sync_writer.write_int32_field(2, timezone_hours)

        writer.write_message_field(128, sync_writer.to_bytes())
        return writer.to_bytes()

    @staticmethod
    def base_heartbeat(magic_random: int) -> bytes:
        writer = ProtobufWriter()
        writer.write_int32_field(1, DevCfgCommandId.BASE_CONN_HEART_BEAT)
        writer.write_int32_field(2, magic_random)
        writer.write_message_field(13, b"")
        return writer.to_bytes()


class G2SettingProto:
    @staticmethod
    def request_info(magic_random: int) -> bytes:
        request_writer = ProtobufWriter()
        request_writer.write_int32_field(1, 1)

        writer = ProtobufWriter()
        writer.write_int32_field(1, 2)
        writer.write_int32_field(2, magic_random)
        writer.write_message_field(4, request_writer.to_bytes())
        return writer.to_bytes()

    @staticmethod
    def universe_settings(magic_random: int) -> bytes:
        writer = ProtobufWriter()
        writer.write_int32_field(1, 1)
        writer.write_int32_field(2, magic_random)
        writer.write_message_field(
            3,
            bytes(
                [
                    0x4A,
                    0x0A,
                    0x08,
                    0x00,
                    0x10,
                    0x00,
                    0x18,
                    0x01,
                    0x20,
                    0x00,
                    0x28,
                    0x01,
                ]
            ),
        )
        return writer.to_bytes()

    @staticmethod
    def gesture_control_list(magic_random: int) -> bytes:
        writer = ProtobufWriter()
        writer.write_int32_field(1, 1)
        writer.write_int32_field(2, magic_random)
        writer.write_message_field(
            3,
            bytes(
                [
                    0x52,
                    0x18,
                    0x0A,
                    0x06,
                    0x08,
                    0x00,
                    0x10,
                    0x00,
                    0x18,
                    0x00,
                    0x0A,
                    0x06,
                    0x08,
                    0x00,
                    0x10,
                    0x01,
                    0x18,
                    0x00,
                    0x0A,
                    0x06,
                    0x08,
                    0x00,
                    0x10,
                    0x02,
                    0x18,
                    0x00,
                ]
            ),
        )
        return writer.to_bytes()


class OnboardingProto:
    @staticmethod
    def skip_onboarding(magic_random: int) -> bytes:
        config = ProtobufWriter()
        config.write_int32_field(1, 4)

        writer = ProtobufWriter()
        writer.write_int32_field(1, 1)
        writer.write_int32_field(2, magic_random)
        writer.write_message_field(3, config.to_bytes())
        return writer.to_bytes()


class EvenAIProto:
    @staticmethod
    def set_hey_even(magic_random: int, enabled: bool) -> bytes:
        config = ProtobufWriter()
        config.write_int32_field(1, 1 if enabled else 0)
        config.write_int32_field(2, 80)

        writer = ProtobufWriter()
        writer.write_int32_field(1, 10)
        writer.write_int32_field(2, magic_random)
        writer.write_message_field(13, config.to_bytes())
        return writer.to_bytes()


class GestureProto:
    @staticmethod
    def init(magic_random: int) -> bytes:
        writer = ProtobufWriter()
        writer.write_int32_field(1, 0)
        writer.write_int32_field(2, magic_random)
        return writer.to_bytes()


class UiSettingProto:
    @staticmethod
    def query(magic_random: int) -> bytes:
        writer = ProtobufWriter()
        writer.write_int32_field(1, 2)
        writer.write_int32_field(2, magic_random)
        writer.write_message_field(4, bytes([0x08, 0x01, 0x10, 0x00]))
        return writer.to_bytes()


class TeleprompterProto:
    @staticmethod
    def finish_config(magic_random: int) -> bytes:
        writer = ProtobufWriter()
        writer.write_int32_field(1, 1)
        writer.write_int32_field(2, magic_random)
        writer.write_message_field(3, bytes([0x08, 0x04]))
        return writer.to_bytes()


class EvenHubCtrlProto:
    @staticmethod
    def init(magic_random: int) -> bytes:
        writer = ProtobufWriter()
        writer.write_int32_field(1, 1)
        writer.write_int32_field(2, magic_random)
        writer.write_message_field(3, b"")
        return writer.to_bytes()


class CalendarProto:
    @staticmethod
    def config(magic_random: int) -> bytes:
        writer = ProtobufWriter()
        writer.write_int32_field(1, 1)
        writer.write_int32_field(2, magic_random)
        writer.write_message_field(3, bytes([0x08, 0x01, 0x10, 0x01, 0x18, 0x05, 0x28, 0x01]))
        return writer.to_bytes()


class DashboardProto:
    @staticmethod
    def init_display(magic_random: int) -> bytes:
        display = ProtobufWriter()
        display.write_int32_field(1, 4)
        display.write_int32_field(2, 3)
        display.write_message_field(3, bytes([1, 2, 3]))
        display.write_int32_field(4, 4)
        display.write_message_field(5, bytes([1, 3, 2, 2]))
        display.write_int32_field(6, 1)
        display.write_int32_field(7, 1)

        receive = ProtobufWriter()
        receive.write_message_field(2, display.to_bytes())

        writer = ProtobufWriter()
        writer.write_int32_field(1, 2)
        writer.write_int32_field(2, magic_random)
        writer.write_message_field(4, receive.to_bytes())
        return writer.to_bytes()

    @staticmethod
    def request_news(magic_random: int) -> bytes:
        writer = ProtobufWriter()
        writer.write_int32_field(1, 5)
        writer.write_int32_field(2, magic_random)
        writer.write_message_field(7, bytes([0x08, 0x01]))
        return writer.to_bytes()

    @staticmethod
    def app_request_news(magic_random: int) -> bytes:
        writer = ProtobufWriter()
        writer.write_int32_field(1, 7)
        writer.write_int32_field(2, magic_random)
        writer.write_message_field(9, bytes([0x08, 0x01]))
        return writer.to_bytes()


class EvenBLETransport:
    @staticmethod
    def build_packets(sync_id: int, service_id: int, payload: bytes, reserve_flag: bool = False) -> list[bytes]:
        chunks = [payload[i : i + G2BLE.MAX_PACKET_PAYLOAD] for i in range(0, len(payload), G2BLE.MAX_PACKET_PAYLOAD)]
        if not chunks:
            chunks = [b""]
        if len(chunks[-1]) == G2BLE.MAX_PACKET_PAYLOAD:
            chunks.append(b"")

        total_packets = len(chunks)
        crc = calc_crc16(payload)
        packets: list[bytes] = []
        for index, chunk in enumerate(chunks, start=1):
            is_last = index == total_packets
            payload_length = len(chunk) + (2 if is_last else 0)
            status = 0x20 if reserve_flag else 0x00

            packet = bytearray(
                [
                    G2BLE.HEADER_BYTE,
                    ((G2BLE.DEST_GLASSES << 4) | G2BLE.SOURCE_PHONE) & 0xFF,
                    sync_id & 0xFF,
                    payload_length & 0xFF,
                    total_packets & 0xFF,
                    index & 0xFF,
                    service_id & 0xFF,
                    status & 0xFF,
                ]
            )
            packet.extend(chunk)
            if is_last:
                packet.append(crc & 0xFF)
                packet.append((crc >> 8) & 0xFF)
            packets.append(bytes(packet))
        return packets


class G2SendManager:
    def __init__(self) -> None:
        self._sync_id = 0
        self._magic_random = 0

    def next_sync_id(self) -> int:
        value = self._sync_id
        self._sync_id = (self._sync_id + 1) & 0xFF
        return value

    def next_magic_random(self) -> int:
        value = self._magic_random
        self._magic_random = (self._magic_random + 1) & 0xFF
        return value

    def build_packets(self, service_id: int, payload: bytes, reserve_flag: bool = False) -> list[bytes]:
        return EvenBLETransport.build_packets(self.next_sync_id(), service_id, payload, reserve_flag)


class G2ReceiveManager:
    def __init__(self) -> None:
        self._partials: dict[str, bytearray] = {}

    def handle_packet(self, raw_data: bytes, source_key: str = "") -> tuple[int, bytes] | None:
        if len(raw_data) < 8 or raw_data[0] != G2BLE.HEADER_BYTE:
            return None

        payload_length = raw_data[3]
        expected_length = payload_length + 8
        if len(raw_data) < expected_length:
            return None

        total_packets = raw_data[4]
        serial_number = raw_data[5]
        service_id = raw_data[6]
        status = raw_data[7]
        result_code = (status >> 1) & 0x0F
        if result_code != 0:
            LOGGER.debug("Ignoring packet with result_code=%s", result_code)
            return None

        is_last = serial_number == total_packets
        payload_end = 8 + payload_length - (2 if is_last else 0)
        payload = raw_data[8:payload_end]
        sync_id = raw_data[2]
        key = f"{source_key}-{service_id}-{sync_id}"

        if serial_number > 1:
            if key not in self._partials:
                return None
            self._partials[key].extend(payload)
        elif total_packets > 1:
            self._partials[key] = bytearray(payload)

        if not is_last:
            return None

        if key in self._partials:
            full_payload = bytes(self._partials.pop(key))
        else:
            full_payload = bytes(payload)
        return service_id, full_payload


# ===== END adapted from Mentra-Community/MentraOS/blob/dev/mobile/modules/bluetooth-sdk/android/src/main/java/com/mentra/bluetoothsdk/sgcs/G2.kt =====
@dataclass(slots=True)
class DiscoveredLens:
    side: str
    serial_number: str
    name: str
    device: BLEDevice
    advertisement: AdvertisementData


@dataclass(slots=True)
class ConnectedLens:
    side: str
    serial_number: str
    name: str
    client: BleakClient
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    authenticated: asyncio.Event = field(default_factory=asyncio.Event)


# ===== BEGIN adapted from Mentra-Community/MentraOS/blob/dev/mobile/modules/bluetooth-sdk/android/src/main/java/com/mentra/bluetoothsdk/sgcs/G2.kt =====
# BLE scan manufacturer-data parsing below mirrors the Kotlin reference.
def extract_serial_number(advertisement: AdvertisementData) -> str | None:
    if not advertisement.manufacturer_data:
        return None
    payload = next(iter(advertisement.manufacturer_data.values()), None)
    if payload is None or len(payload) < 14:
        return None
    serial = payload[:14].decode("ascii", errors="ignore")
    serial = re.sub(r"[\x00-\x1F\x7F]", "", serial)
    return serial or None


# ===== END adapted from Mentra-Community/MentraOS/blob/dev/mobile/modules/bluetooth-sdk/android/src/main/java/com/mentra/bluetoothsdk/sgcs/G2.kt =====
class EvenG2HelloApp:
    def __init__(self, *, serial_filter: str | None, text: str, scan_timeout: float, heartbeat_interval: float) -> None:
        self.serial_filter = serial_filter
        self.text = text
        self.scan_timeout = scan_timeout
        self.heartbeat_interval = heartbeat_interval

        self.send_manager = G2SendManager()
        self.receive_manager = G2ReceiveManager()
        self.left: ConnectedLens | None = None
        self.right: ConnectedLens | None = None
        self.startup_page_created = False
        self.text_container_id = 1
        self.stop_event = asyncio.Event()
        self._background_tasks: list[asyncio.Task[None]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._auth_status: dict[str, bool | None] = {"L": None, "R": None}

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.connect_pair()
        await self.run_auth_sequence()
        await self.perform_boot_sequence()
        await self.show_text(self.text)
        self.start_heartbeats()
        LOGGER.info("Hello Worldを表示しました。Heart Beat送信を継続します。Ctrl+C で終了します。")
        await self.stop_event.wait()

    async def close(self) -> None:
        self.stop_event.set()
        for task in self._background_tasks:
            task.cancel()
        for task in self._background_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._background_tasks.clear()

        for lens in [self.left, self.right]:
            if lens is None:
                continue
            with contextlib.suppress(Exception):
                await lens.client.disconnect()

    async def connect_pair(self) -> None:
        left_lens, right_lens = await self.scan_for_pair()
        self.left = await self.connect_lens(left_lens)
        self.right = await self.connect_lens(right_lens)
        LOGGER.info(
            "接続完了: LEFT=%s (%s), RIGHT=%s (%s)",
            self.left.name,
            self.left.client.address,
            self.right.name,
            self.right.client.address,
        )

    # ===== BEGIN adapted from Mentra-Community/MentraOS/blob/dev/mobile/modules/bluetooth-sdk/android/src/main/java/com/mentra/bluetoothsdk/sgcs/G2.kt =====
    # Discovery, auth response parsing, startup sequencing, display updates,
    # and heartbeat packet flow below follow the Kotlin reference. Windows
    # pairing and asyncio glue inside this block are Python-specific additions.
    async def scan_for_pair(self) -> tuple[DiscoveredLens, DiscoveredLens]:
        LOGGER.info("Even G2 を %.1f 秒スキャンします", self.scan_timeout)
        discovered = await BleakScanner.discover(timeout=self.scan_timeout, return_adv=True)
        grouped: dict[str, dict[str, DiscoveredLens]] = defaultdict(dict)

        for device, advertisement in discovered.values():
            name = device.name or advertisement.local_name or ""
            if "G2" not in name:
                continue
            serial_number = extract_serial_number(advertisement)
            if not serial_number:
                continue
            side = "L" if "_L_" in name else "R" if "_R_" in name else None
            if side is None:
                continue
            grouped[serial_number][side] = DiscoveredLens(
                side=side,
                serial_number=serial_number,
                name=name,
                device=device,
                advertisement=advertisement,
            )

        candidates = [
            (serial_number, pair)
            for serial_number, pair in grouped.items()
            if "L" in pair and "R" in pair and (self.serial_filter is None or self.serial_filter in serial_number)
        ]

        if not candidates:
            discovered_serials = ", ".join(sorted(grouped)) or "なし"
            raise RuntimeError(
                f"左右ペアの Even G2 が見つかりませんでした。発見済みシリアル: {discovered_serials}"
            )

        if len(candidates) > 1:
            serials = ", ".join(serial for serial, _ in candidates)
            raise RuntimeError(
                f"複数の Even G2 ペアが見つかりました。--serial で絞ってください: {serials}"
            )

        serial_number, pair = candidates[0]
        LOGGER.info("対象デバイス: %s", serial_number)
        return pair["L"], pair["R"]

    async def connect_lens(self, lens: DiscoveredLens) -> ConnectedLens:
        LOGGER.info("%s 側へ接続します: %s", lens.side, lens.name)
        client_kwargs = {
            "disconnected_callback": self._make_disconnect_callback(lens.side),
            "timeout": CONNECT_TIMEOUT_SECONDS,
        }
        if BLEAKCLIENT_ACCEPTS_PAIR:
            client_kwargs["pair"] = True
            LOGGER.info("%s 側は接続時にOSペアリングを試行します", lens.side)

        client = BleakClient(lens.device, **client_kwargs)
        await client.connect()
        if not BLEAKCLIENT_ACCEPTS_PAIR:
            await self.try_pair(client, lens.side)

        services = client.services

        if services.get_characteristic(G2BLE.WRITE_UUID) is None:
            raise RuntimeError(f"{lens.side} 側で WRITE characteristic が見つかりません")
        if services.get_characteristic(G2BLE.NOTIFY_UUID) is None:
            raise RuntimeError(f"{lens.side} 側で NOTIFY characteristic が見つかりません")

        connected = ConnectedLens(
            side=lens.side,
            serial_number=lens.serial_number,
            name=lens.name,
            client=client,
        )

        await client.start_notify(G2BLE.NOTIFY_UUID, self._make_notify_callback(lens.side))
        if services.get_characteristic(G2BLE.AUDIO_NOTIFY_UUID) is not None:
            await client.start_notify(G2BLE.AUDIO_NOTIFY_UUID, self._make_audio_callback(lens.side))

        return connected

    async def try_pair(self, client: BleakClient, side: str) -> None:
        if not hasattr(client, "pair"):
            return

        try:
            LOGGER.info("%s 側でOSペアリングを試行します", side)
            await client.pair()
            await asyncio.sleep(PAIR_SETTLE_SECONDS)
        except NotImplementedError:
            LOGGER.debug("%s 側ではペアリングAPIが未対応です", side)
        except Exception as exc:
            LOGGER.warning("%s 側のOSペアリングに失敗しました: %s", side, exc)

    def _make_disconnect_callback(self, side: str) -> Callable[[BleakClient], None]:
        def _callback(_: BleakClient) -> None:
            if self._loop is None:
                LOGGER.warning("%s 側が切断されました", side)
                self.stop_event.set()
                return

            def _handle_disconnect() -> None:
                LOGGER.warning("%s 側が切断されました", side)
                self.stop_event.set()

            self._loop.call_soon_threadsafe(_handle_disconnect)

        return _callback

    def _make_notify_callback(self, side: str) -> Callable[[object, bytearray], None]:
        def _callback(_: object, data: bytearray) -> None:
            payload = bytes(data)
            if self._loop is None:
                self.handle_notify_data(payload, side)
                return
            self._loop.call_soon_threadsafe(self.handle_notify_data, payload, side)

        return _callback

    def _make_audio_callback(self, side: str) -> Callable[[object, bytearray], None]:
        def _callback(_: object, data: bytearray) -> None:
            LOGGER.debug("%s AUDIO notify: %d bytes", side, len(data))

        return _callback

    def handle_notify_data(self, data: bytes, side: str) -> None:
        result = self.receive_manager.handle_packet(data, side)
        if result is None:
            return
        service_id, payload = result
        if service_id == ServiceID.DEVICE_SETTINGS:
            self.handle_devsettings_response(payload, side)
            return
        LOGGER.debug("%s 側 service=0x%02X payload=%s", side, service_id, payload[:32].hex())

    def handle_devsettings_response(self, payload: bytes, side: str) -> None:
        fields = ProtobufReader(payload).parse_fields()
        command_id = int(fields.get(1, -1))
        if command_id == DevCfgCommandId.BASE_CONN_HEART_BEAT:
            return

        LOGGER.debug("%s 側 devsettings response cmd=%s payload=%s", side, command_id, payload[:32].hex())
        if command_id != DevCfgCommandId.AUTHENTICATION:
            return

        auth_blob = fields.get(3)
        if not isinstance(auth_blob, bytes):
            return

        auth_fields = ProtobufReader(auth_blob).parse_fields()
        sec_auth = bool(int(auth_fields.get(1, 0)))
        self._auth_status[side] = sec_auth
        LOGGER.info("%s 側 AUTHENTICATION 応答 secAuth=%s", side, sec_auth)
        if not sec_auth:
            return

        if side == "L" and self.left is not None:
            self.left.authenticated.set()
        if side == "R" and self.right is not None:
            self.right.authenticated.set()

    async def run_auth_sequence(self) -> None:
        if self.left is None or self.right is None:
            raise RuntimeError("左右のレンズが未接続です")

        self.left.authenticated.clear()
        self.right.authenticated.clear()
        self._auth_status["L"] = None
        self._auth_status["R"] = None

        LOGGER.info("認証シーケンスを開始します")
        await self.send_devsettings_command(
            DevSettingsProto.auth_cmd(self.send_manager.next_magic_random()),
            left=True,
            right=False,
        )
        await asyncio.sleep(AUTH_STEP_DELAY_SECONDS)

        await self.send_devsettings_command(
            DevSettingsProto.auth_cmd(self.send_manager.next_magic_random()),
            left=False,
            right=True,
        )
        await asyncio.sleep(AUTH_STEP_DELAY_SECONDS)

        await self.send_devsettings_command(
            DevSettingsProto.pipe_role_change(self.send_manager.next_magic_random()),
            left=False,
            right=True,
        )
        await asyncio.sleep(AUTH_STEP_DELAY_SECONDS)

        await self.send_devsettings_command(
            DevSettingsProto.time_sync(self.send_manager.next_magic_random()),
            left=False,
            right=True,
        )

        try:
            await asyncio.wait_for(
                asyncio.gather(self.left.authenticated.wait(), self.right.authenticated.wait()),
                timeout=5.0,
            )
        except asyncio.TimeoutError as exc:
            auth_state = ", ".join(f"{side}={self._auth_status[side]}" for side in ["L", "R"])
            if self.stop_event.is_set():
                raise RuntimeError(
                    f"認証完了前にデバイスが切断されました。AUTH応答: {auth_state}. "
                    "Windowsでは事前ペアリングが必要な場合があります。Bluetooth設定から左右レンズを削除して再実行してください。"
                ) from exc
            raise RuntimeError(
                f"認証がタイムアウトしました。AUTH応答: {auth_state}. "
                "secAuth=False の場合は Windows 側のBLEペアリングが不足している可能性があります。"
            ) from exc

        LOGGER.info("左右の認証が完了しました")
        await asyncio.sleep(AUTH_STEP_DELAY_SECONDS)

    async def perform_boot_sequence(self) -> None:
        LOGGER.info("Even G2 の起動シーケンスを送信します")

        commands = [
            (ServiceID.ONBOARDING, OnboardingProto.skip_onboarding(self.send_manager.next_magic_random()), True),
            (ServiceID.EVEN_AI, EvenAIProto.set_hey_even(self.send_manager.next_magic_random(), False), True),
            (ServiceID.G2_SETTING, G2SettingProto.universe_settings(self.send_manager.next_magic_random()), True),
            (ServiceID.GESTURE_CTRL, GestureProto.init(self.send_manager.next_magic_random()), True),
            (0x0C, UiSettingProto.query(self.send_manager.next_magic_random()), True),
            (0x10, TeleprompterProto.finish_config(self.send_manager.next_magic_random()), True),
            (ServiceID.EVEN_HUB_CTRL, EvenHubCtrlProto.init(self.send_manager.next_magic_random()), True),
            (0x04, CalendarProto.config(self.send_manager.next_magic_random()), True),
            (ServiceID.DASHBOARD, DashboardProto.init_display(self.send_manager.next_magic_random()), True),
            (ServiceID.DASHBOARD, DashboardProto.request_news(self.send_manager.next_magic_random()), True),
            (ServiceID.G2_SETTING, G2SettingProto.gesture_control_list(self.send_manager.next_magic_random()), True),
            (ServiceID.DASHBOARD, DashboardProto.app_request_news(self.send_manager.next_magic_random()), True),
            (ServiceID.G2_SETTING, G2SettingProto.request_info(self.send_manager.next_magic_random()), True),
        ]

        for service_id, payload, reserve_flag in commands:
            await self.send_service_command(int(service_id), payload, reserve_flag=reserve_flag)
            await asyncio.sleep(INIT_COMMAND_GAP_SECONDS)

    async def show_text(self, text: str) -> None:
        text_container = EvenHubProto.text_container_property(
            x=0,
            y=0,
            width=576,
            height=288,
            padding_length=4,
            container_id=self.text_container_id,
            container_name="text-main",
            is_event_capture=True,
            content=text,
        )

        if not self.startup_page_created:
            payload = EvenHubProto.create_page_message(
                text_containers=[text_container],
                magic_random=self.send_manager.next_magic_random(),
            )
            self.startup_page_created = True
        else:
            payload = EvenHubProto.update_text_message(
                container_id=self.text_container_id,
                content_offset=0,
                content_length=len(text.encode("utf-8")),
                content=text,
            )

        await self.send_evenhub_command(payload)

    def start_heartbeats(self) -> None:
        self._background_tasks.append(asyncio.create_task(self._heartbeat_loop(self.send_evenhub_heartbeat)))
        self._background_tasks.append(asyncio.create_task(self._heartbeat_loop(self.send_devsettings_heartbeat)))

    async def _heartbeat_loop(self, callback: Callable[[], asyncio.Future | asyncio.Task | object]) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self.heartbeat_interval)
                return
            except asyncio.TimeoutError:
                await callback()

    async def send_evenhub_heartbeat(self) -> None:
        await self.send_evenhub_command(EvenHubProto.heartbeat_message())
        LOGGER.debug("EvenHub heartbeat sent")

    async def send_devsettings_heartbeat(self) -> None:
        await self.send_devsettings_command(
            DevSettingsProto.base_heartbeat(self.send_manager.next_magic_random()),
            left=False,
            right=True,
        )
        LOGGER.debug("DevSettings heartbeat sent")

    async def send_evenhub_command(self, payload: bytes) -> None:
        await self.send_service_command(ServiceID.EVEN_HUB, payload, reserve_flag=True)

    async def send_devsettings_command(self, payload: bytes, *, left: bool, right: bool) -> None:
        await self.send_service_command(ServiceID.DEVICE_SETTINGS, payload, reserve_flag=False, left=left, right=right)

    async def send_service_command(
        self,
        service_id: int,
        payload: bytes,
        *,
        reserve_flag: bool,
        left: bool = False,
        right: bool = True,
    ) -> None:
        packets = self.send_manager.build_packets(service_id, payload, reserve_flag)
        targets: list[ConnectedLens] = []
        if left and self.left is not None:
            targets.append(self.left)
        if right and self.right is not None:
            targets.append(self.right)
        if not targets:
            raise RuntimeError("送信先のレンズがありません")
        await asyncio.gather(*(self._write_packets(target, packets) for target in targets))

    async def _write_packets(self, lens: ConnectedLens, packets: list[bytes]) -> None:
        async with lens.write_lock:
            for index, packet in enumerate(packets):
                await lens.client.write_gatt_char(G2BLE.WRITE_UUID, packet, response=False)
                if index + 1 < len(packets):
                    await asyncio.sleep(WRITE_GAP_SECONDS)


# ===== END adapted from Mentra-Community/MentraOS/blob/dev/mobile/modules/bluetooth-sdk/android/src/main/java/com/mentra/bluetoothsdk/sgcs/G2.kt =====
def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Even G2 に接続し Hello World を表示する BLE アプリ")
    parser.add_argument("message", nargs="*", help="表示する文字列。複数語を渡した場合は空白で連結します")
    parser.add_argument("--serial", help="接続対象のシリアル番号の一部。複数の G2 が近くにある場合に指定します")
    parser.add_argument("--text", help="表示するテキスト。位置引数の代替です")
    parser.add_argument("--scan-timeout", type=float, default=8.0, help="スキャン秒数")
    parser.add_argument("--heartbeat-seconds", type=float, default=5.0, help="Heart Beat 送信間隔")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="ログレベル",
    )
    return parser


def resolve_display_text(args: argparse.Namespace) -> str:
    if args.message:
        return " ".join(args.message)
    if args.text:
        return args.text
    return "Hello World"


async def async_main(args: argparse.Namespace) -> None:
    app = EvenG2HelloApp(
        serial_filter=args.serial,
        text=resolve_display_text(args),
        scan_timeout=args.scan_timeout,
        heartbeat_interval=args.heartbeat_seconds,
    )
    try:
        await app.run()
    finally:
        await app.close()


def main() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        LOGGER.info("終了します")
    except Exception as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()