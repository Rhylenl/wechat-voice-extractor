import importlib.util
import hashlib
import struct
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "wechat_voice_extract.py"


def load_script():
    spec = importlib.util.spec_from_file_location("wechat_voice_extract", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_minidump(memory_ranges):
    """Build the smallest useful Memory64ListStream minidump."""
    directory_rva = 0x20
    memory64_rva = 0x40
    base_rva = memory64_rva + 16 + len(memory_ranges) * 16
    data = bytearray(base_rva)
    data[0:4] = b"MDMP"
    struct.pack_into("<I", data, 8, 1)
    struct.pack_into("<I", data, 12, directory_rva)
    struct.pack_into("<III", data, directory_rva, 9, 16 + len(memory_ranges) * 16, memory64_rva)
    struct.pack_into("<QQ", data, memory64_rva, len(memory_ranges), base_rva)

    for index, (virtual_address, payload) in enumerate(memory_ranges):
        struct.pack_into("<QQ", data, memory64_rva + 16 + index * 16, virtual_address, len(payload))
        data.extend(payload)
    return bytes(data)


class MinidumpTests(unittest.TestCase):
    def test_parses_memory64_ranges_and_maps_file_offsets(self):
        module = load_script()
        raw = make_minidump([(0x1000, b"abc"), (0x2000, b"defgh")])

        ranges = module.parse_memory64_ranges(raw)

        self.assertEqual(
            [(item.virtual_address, item.size, item.file_offset) for item in ranges],
            [(0x1000, 3, 0x70), (0x2000, 5, 0x73)],
        )


class MetadataTests(unittest.TestCase):
    def test_extracts_complete_speex_record_without_confusing_thumb_fields(self):
        module = load_script()
        record = (
            b"datatype: 3 dataid: 11111111111111111111111111111111 "
            b"datafmt: speex duration: 224319 thumbfullsize: 0 thumbfullmd5:  "
            b"fullsize: 672120, fullmd5: 22222222222222222222222222222222 "
            b"head256md5: 33333333333333333333333333333333 "
            b"localpathinfo: [[datapath: E:\\weixin\\business\\favorite\\temp\\"
            b"sample-speex.speex_temp displaypath: ]]"
        )

        items = module.extract_voice_metadata(record, r"E:\weixin")

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.duration_ms, 224319)
        self.assertEqual(item.full_size, 672120)
        self.assertEqual(item.full_md5, "22222222222222222222222222222222")
        self.assertEqual(item.head256_md5, "33333333333333333333333333333333")
        self.assertTrue(item.data_path.endswith(".speex_temp"))

    def test_keeps_note_html_fields_out_of_following_silk_voice(self):
        module = load_script()
        record = (
            b'<favitem type="18"><datalist count="2">'
            b'<dataitem htmlid="WeNoteHtmlFile" dataid="44444444444444444444444444444444" '
            b'datatype="8"><datafmt>.htm</datafmt>'
            b'<fullmd5>55555555555555555555555555555555</fullmd5>'
            b'<head256md5>55555555555555555555555555555555</head256md5>'
            b'<fullsize>115</fullsize></dataitem>'
            b'<dataitem htmlid="WeNote_0" dataid="66666666666666666666666666666666" '
            b'datatype="20"><datafmt>silk</datafmt>'
            b'<fullmd5>77777777777777777777777777777777</fullmd5>'
            b'<duration>12820</duration>'
            b'<head256md5>88888888888888888888888888888888</head256md5>'
            b'<fullsize>24600</fullsize></dataitem></datalist></favitem> '
            b'datatype: 20 dataid: 66666666666666666666666666666666 '
            b'datafmt: silk duration: 12820 thumbfullsize: 0 thumbfullmd5:  '
            b'fullsize: 24600, fullmd5: 77777777777777777777777777777777 '
            b'localpathinfo: [[datapath: E:\\weixin\\business\\favorite\\temp\\'
            b'sample-silk.silk_temp displaypath: ]]'
        )

        items = module.plausible_metadata(
            module.extract_voice_metadata(record, r"E:\weixin")
        )

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.data_format, "silk")
        self.assertEqual(item.duration_ms, 12820)
        self.assertEqual(item.full_size, 24600)
        self.assertEqual(item.full_md5, "77777777777777777777777777777777")
        self.assertEqual(item.head256_md5, "88888888888888888888888888888888")
        self.assertTrue(item.data_path.endswith(".silk_temp"))

    def test_filters_metadata_by_requested_duration_before_memory_scan(self):
        module = load_script()
        old = module.VoiceMetadata(
            duration_ms=224319,
            full_size=672120,
            full_md5="a" * 32,
            head256_md5="b" * 32,
            data_path=r"E:\weixin\old.speex_temp",
        )
        target = module.VoiceMetadata(
            duration_ms=12820,
            full_size=24600,
            full_md5="c" * 32,
            head256_md5="d" * 32,
            data_path=r"E:\weixin\target.silk_temp",
            data_format="silk",
        )

        selected = module.filter_metadata_by_duration([old, target], 13)

        self.assertEqual(selected, [target])


class VoiceLocationTests(unittest.TestCase):
    def test_bootstraps_from_head_md5_at_heap_payload_offset(self):
        module = load_script()
        voice = (b"fixed-speex-head" * 16)[:256] + b"voice-payload" * 64
        voice = voice[:960]
        memory = b"H" * 0x80 + voice + b"Z" * 128
        raw = make_minidump([(0x500000, memory)])
        ranges = module.parse_memory64_ranges(raw)
        metadata = module.VoiceMetadata(
            duration_ms=320,
            full_size=len(voice),
            full_md5=hashlib.md5(voice).hexdigest(),
            head256_md5=hashlib.md5(voice[:256]).hexdigest(),
            data_path=r"E:\weixin\target.speex_temp",
        )

        matches = module.find_voice_matches(raw, ranges, [metadata])

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].virtual_address, 0x500080)
        self.assertEqual(matches[0].head_bytes, voice[:256])

    def test_falls_back_to_bootstrap_when_saved_head_is_stale(self):
        module = load_script()
        voice = (b"new-speex-head" * 20)[:256] + b"payload" * 80
        memory = b"H" * 0x80 + voice
        raw = make_minidump([(0x900000, memory)])
        ranges = module.parse_memory64_ranges(raw)
        metadata = module.VoiceMetadata(
            duration_ms=len(voice) // 60 * 20,
            full_size=len(voice),
            full_md5=hashlib.md5(voice).hexdigest(),
            head256_md5=hashlib.md5(voice[:256]).hexdigest(),
            data_path=r"E:\weixin\new.speex_temp",
        )

        matches = module.find_voice_matches(raw, ranges, [metadata], known_head=b"X" * 256)

        self.assertEqual(len(matches), 1)

    def test_finds_silk_magic_beyond_bootstrap_window_and_off_alignment(self):
        module = load_script()
        voice = b"\x02#!SILK_V3" + b"\x0c\x00" + b"silk-frame" * 600
        offset = 0x73004
        memory = b"H" * offset + voice + b"Z" * 128
        raw = make_minidump([(0xA00000, memory)])
        ranges = module.parse_memory64_ranges(raw)
        metadata = module.VoiceMetadata(
            duration_ms=3462,
            full_size=len(voice),
            full_md5=hashlib.md5(voice).hexdigest(),
            head256_md5=hashlib.md5(voice[:256]).hexdigest(),
            data_path=r"E:\weixin\target.silk_temp",
            data_format="silk",
        )

        matches = module.find_voice_matches(raw, ranges, [metadata])

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].virtual_address, 0xA00000 + offset)


class ProcessSelectionTests(unittest.TestCase):
    def test_prefers_visible_top_level_weixin_over_larger_player_helper(self):
        module = load_script()
        processes = [
            module.ProcessInfo(1001, 100, 500_000_000, True, "微信", False),
            module.ProcessInfo(1002, 1001, 800_000_000, False, "", True),
        ]

        selected = module.choose_main_weixin(processes)

        self.assertEqual(selected.pid, 1001)


class DumpCommandTests(unittest.TestCase):
    def test_uses_cmd_batch_form_that_preserves_full_argument(self):
        module = load_script()

        batch = module.build_full_dump_batch(
            4242,
            r"C:\Dump\target.dmp",
            r"TESTHOST\TestUser",
        )

        self.assertIn(
            "comsvcs.dll, MiniDump 4242 C:\\Dump\\target.dmp full",
            batch,
        )
        self.assertIn(
            'icacls.exe C:\\Dump\\target.dmp /grant "TESTHOST\\TestUser":F',
            batch,
        )
        self.assertNotIn("Start-Process", batch)


if __name__ == "__main__":
    unittest.main()

