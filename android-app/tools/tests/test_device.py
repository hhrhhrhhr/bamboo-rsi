"""Host tests use scripted ADB output, never a game or real device."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("bamboo_device", Path(__file__).parents[1] / "device.py")
device = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(device)


class ScriptedAdb(device.Adb):
    def __init__(self, outputs):
        super().__init__("adb")
        self.outputs = outputs
        self.calls = []

    def call(self, *args, **kwargs):
        self.calls.append(args)
        return self.outputs[args]


class DeviceTests(unittest.TestCase):
    def test_selection_requires_unambiguous_authorized_device(self):
        for listing in ("List of devices attached\n", "a\tunauthorized\n", "a\tdevice\nb\tdevice\n"):
            with self.assertRaises(device.DeviceError):
                ScriptedAdb({("devices",): listing}).select(None)
        adb = ScriptedAdb({("devices",): "List of devices attached\na\tdevice\nb\toffline\n"})
        adb.select("a")
        self.assertEqual("a", adb.serial)
        with self.assertRaises(device.DeviceError):
            adb.select("b")

    def test_doctor_only_reads_and_redacts_device_identifiers(self):
        outputs = {
            ("shell", "getprop", "ro.build.version.sdk"): "35\n",
            ("shell", "dumpsys", "package", device.APP): "versionCode=2 minSdk=30\nversionName=0.2.0-dev",
            ("shell", "dumpsys", "package", device.GAME): "versionCode=123\nversionName=test",
            ("shell", "settings", "get", "secure", "enabled_accessibility_services"): "another/private:" + device.APP + "/.GameAccessibility",
            ("shell", "df", "-k", "/data"): "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/block/test 6000000 2000000 4000000 33% /data",
            ("shell", "getprop", "ro.product.model"): "Test model",
            ("shell", "getprop", "ro.product.cpu.abi"): "arm64-v8a",
            ("shell", "getprop", "ro.kernel.qemu"): "0",
        }
        adb = ScriptedAdb(outputs)
        adb.serial = "test-private-device-serial"
        report = device.inspect(adb)
        self.assertEqual([], report["pending"])
        self.assertFalse(report["live_game_acceptance"])
        self.assertNotIn(adb.serial, str(report))
        self.assertNotIn("another/private", str(report))
        self.assertTrue(all(call[0] == "shell" and call[1] in ("getprop", "dumpsys", "settings", "df") for call in adb.calls))

    def test_unknown_storage_and_package_are_not_success(self):
        self.assertIsNone(device.data_free_bytes("unexpected response"))
        self.assertFalse(device.package_version("Unable to find package")["installed"])
        self.assertEqual(102400, device.data_free_bytes("/dev/test 200 100 100 50% /data"))

    def test_deployment_refuses_running_app_game_or_child_process(self):
        for name in (device.APP, device.GAME, device.APP + ":service", device.GAME + ":service"):
            adb = ScriptedAdb({("shell", "ps", "-A", "-o", "NAME"): "NAME\n" + name})
            with self.assertRaises(device.DeviceError):
                device.require_idle(adb)
            self.assertEqual(1, len(adb.calls))
        device.require_idle(ScriptedAdb({("shell", "ps", "-A", "-o", "NAME"): "NAME\nsystem_server"}))

    def test_install_preserves_data_without_automatic_permission_grants(self):
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "app.apk"
            apk.write_bytes(b"synthetic-test-not-an-apk")
            command = ("install", "-r", str(apk.resolve()))
            adb = ScriptedAdb({command: "Performing Streamed Install\nSuccess\n"})
            self.assertEqual(64, len(device.install(adb, apk)))
            self.assertEqual([command], adb.calls)
            adb.outputs[command] = "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]"
            with self.assertRaises(device.DeviceError):
                device.install(adb, apk)

    def test_instrumentation_requires_two_completed_tests_and_no_failure(self):
        command = ("shell", "am", "instrument", "-w", "-e", "class", device.APP + ".DeviceSmokeTest", device.RUNNER)
        adb = ScriptedAdb({command: "INSTRUMENTATION_RESULT: stream=\nTime: 1\nOK (2 tests)\nINSTRUMENTATION_CODE: -1"})
        self.assertEqual(2, device.smoke(adb))
        for output in ("OK (0 tests)", "OK (2 tests)\nINSTRUMENTATION_FAILED", "Process crashed", "OK (1 test)"):
            adb.outputs[command] = output
            with self.assertRaises(device.DeviceError):
                device.smoke(adb)

    def test_process_error_does_not_echo_device_output(self):
        with patch.object(device.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "private", "secret")):
            with self.assertRaisesRegex(device.DeviceError, "^ADB operation failed: shell$"):
                device.Adb("adb", "selected").call("shell", "ps")


if __name__ == "__main__":
    unittest.main()
