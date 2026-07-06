"""Register parser for Janitza UMG 512-PRO Modbus data."""

import struct
from typing import List, Optional, Any, Dict


class RegisterParser:
    """
    Parser for Janitza Modbus register data.

    Supports various data types:
    - float (32-bit IEEE 754)
    - int32 (signed 32-bit)
    - uint32 (unsigned 32-bit)
    - int16 (signed 16-bit)
    - uint16 (unsigned 16-bit)
    - int64/long64 (signed 64-bit)
    - double (64-bit IEEE 754)
    """

    # Number of 16-bit registers per data type
    REGISTER_COUNTS = {
        'float': 2,
        'float32': 2,
        'int32': 2,
        'uint32': 2,
        'int16': 1,
        'uint16': 1,
        'short': 1,
        'int64': 4,
        'long64': 4,
        'uint64': 4,
        'double': 4,
    }

    # byte-order name -> (byteswap_within_word, word_swap). Each 16-bit register is
    # big-endian on the Modbus wire; "byteswap" swaps the two bytes inside a word,
    # "word_swap" reverses the word order. The four combinations cover the classic
    # ABCD / CDAB / BADC / DCBA orderings. 'big'/'little' keep the historical
    # word-only meaning (big=ABCD, little=CDAB) for byte-for-byte back-compat.
    _ORDER_MAP = {
        'big': (False, False), 'abcd': (False, False), 'be': (False, False), 'bigendian': (False, False),
        'little': (False, True), 'cdab': (False, True), 'wordswap': (False, True),
        'badc': (True, False), 'byteswap': (True, False),
        'dcba': (True, True), 'le': (True, True), 'littleendian': (True, True),
    }

    @classmethod
    def resolve_order(cls, byte_order: str):
        """Return (byteswap, word_swap) for a byte-order name; unknown -> big-endian."""
        key = str(byte_order or 'big').lower().replace('-', '').replace('_', '').replace(' ', '')
        return cls._ORDER_MAP.get(key, (False, False))

    def __init__(self, byte_order: str = 'big'):
        """
        Initialize parser.

        Args:
            byte_order: 'big'/'abcd' (default), 'little'/'cdab', 'badc', or 'dcba'.
        """
        self.byte_order = byte_order
        self._byteswap, self._word_swap = self.resolve_order(byte_order)
        self._endian_prefix = '>' if byte_order == 'big' else '<'

    def _canon(self, registers: List[int]) -> List[int]:
        """Normalize a value's registers to canonical big-endian, high word first,
        so every decode below is a plain big-endian read."""
        regs = [int(r) & 0xffff for r in registers]
        if self._byteswap:
            regs = [((r & 0xff) << 8) | ((r >> 8) & 0xff) for r in regs]
        if self._word_swap:
            regs.reverse()
        return regs

    def get_register_count(self, data_type: str) -> int:
        """Get number of 16-bit registers needed for a data type."""
        return self.REGISTER_COUNTS.get(data_type.lower(), 2)

    def parse_value(self, registers: List[int], data_type: str) -> Optional[Any]:
        """
        Parse register values according to data type.

        Args:
            registers: List of 16-bit register values
            data_type: Data type string

        Returns:
            Parsed value or None if parsing fails
        """
        if not registers:
            return None

        data_type = data_type.lower()

        try:
            if data_type in ('float', 'float32'):
                return self._parse_float(registers)
            elif data_type == 'double':
                return self._parse_double(registers)
            elif data_type == 'int32':
                return self._parse_int32(registers)
            elif data_type == 'uint32':
                return self._parse_uint32(registers)
            elif data_type in ('int16', 'short'):
                return self._parse_int16(registers)
            elif data_type == 'uint16':
                return self._parse_uint16(registers)
            elif data_type in ('int64', 'long64'):
                return self._parse_int64(registers)
            elif data_type == 'uint64':
                return self._parse_uint64(registers)
            else:
                # Default to float
                return self._parse_float(registers)
        except Exception:
            return None

    def _parse_float(self, registers: List[int]) -> Optional[float]:
        """Parse 32-bit IEEE 754 float from 2 registers."""
        if len(registers) < 2:
            return None
        r = self._canon(registers[:2])
        value = struct.unpack('>f', struct.pack('>HH', r[0], r[1]))[0]
        if value != value or abs(value) == float('inf'):   # NaN / Inf
            return None
        return value

    def _parse_double(self, registers: List[int]) -> Optional[float]:
        """Parse 64-bit IEEE 754 double from 4 registers."""
        if len(registers) < 4:
            return None
        r = self._canon(registers[:4])
        value = struct.unpack('>d', struct.pack('>HHHH', r[0], r[1], r[2], r[3]))[0]
        if value != value or abs(value) == float('inf'):
            return None
        return value

    def _parse_int32(self, registers: List[int]) -> Optional[int]:
        """Parse signed 32-bit integer from 2 registers."""
        if len(registers) < 2:
            return None
        r = self._canon(registers[:2])
        return struct.unpack('>i', struct.pack('>HH', r[0], r[1]))[0]

    def _parse_uint32(self, registers: List[int]) -> Optional[int]:
        """Parse unsigned 32-bit integer from 2 registers."""
        if len(registers) < 2:
            return None
        r = self._canon(registers[:2])
        return (r[0] << 16) | r[1]

    def _parse_int16(self, registers: List[int]) -> Optional[int]:
        """Parse signed 16-bit integer from 1 register."""
        if len(registers) < 1:
            return None
        value = self._canon(registers[:1])[0]
        if value >= 32768:
            value -= 65536
        return value

    def _parse_uint16(self, registers: List[int]) -> Optional[int]:
        """Parse unsigned 16-bit integer from 1 register."""
        if len(registers) < 1:
            return None
        return self._canon(registers[:1])[0]

    def _parse_int64(self, registers: List[int]) -> Optional[int]:
        """Parse signed 64-bit integer from 4 registers."""
        if len(registers) < 4:
            return None
        r = self._canon(registers[:4])
        return struct.unpack('>q', struct.pack('>HHHH', r[0], r[1], r[2], r[3]))[0]

    def _parse_uint64(self, registers: List[int]) -> Optional[int]:
        """Parse unsigned 64-bit integer from 4 registers."""
        if len(registers) < 4:
            return None
        r = self._canon(registers[:4])
        return ((r[0] << 48) | (r[1] << 32) | (r[2] << 16) | r[3])

    def parse_registers(self, all_registers: Dict[int, List[int]],
                        register_configs: List[Dict]) -> Dict[int, Any]:
        """
        Parse multiple registers based on configuration.

        Args:
            all_registers: Dict mapping address -> register values
            register_configs: List of register configurations with address, data_type

        Returns:
            Dict mapping address -> parsed value
        """
        results = {}

        for config in register_configs:
            address = config['address']
            data_type = config.get('data_type', 'float')

            if address in all_registers:
                value = self.parse_value(all_registers[address], data_type)
                results[address] = value

        return results
