"""Windows audio device utilities (Stereo Mix enable via registry)."""
import platform
import sys

CAPTURE_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
# PKEY_Device_FriendlyName
FRIENDLY_NAME_PROP = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
DEVICE_STATE_ACTIVE = 1


def is_admin() -> bool:
    if platform.system() != "Windows":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _find_stereo_mix_guid() -> str | None:
    """Return the registry GUID key for the Stereo Mix capture endpoint, or None."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, CAPTURE_KEY) as root:
            i = 0
            while True:
                try:
                    guid = winreg.EnumKey(root, i)
                    i += 1
                except OSError:
                    break
                props_path = rf"{CAPTURE_KEY}\{guid}\Properties"
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, props_path) as props:
                        name, _ = winreg.QueryValueEx(props, FRIENDLY_NAME_PROP)
                    if "stereo mix" in name.lower() or "стерео" in name.lower():
                        return guid
                except (FileNotFoundError, OSError):
                    continue
    except Exception:
        pass
    return None


def enable_stereo_mix() -> tuple[bool, str]:
    """
    Enable the Stereo Mix capture endpoint via registry.
    Returns (success, message). Requires administrator rights.
    """
    if platform.system() != "Windows":
        return False, "Только Windows"

    import winreg

    guid = _find_stereo_mix_guid()
    if not guid:
        return False, "Стерео микшер не найден в реестре"

    device_path = rf"{CAPTURE_KEY}\{guid}"
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            device_path,
            0,
            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
        ) as key:
            winreg.SetValueEx(key, "DeviceState", 0, winreg.REG_DWORD, DEVICE_STATE_ACTIVE)
        return True, "Стерео микшер включён"
    except PermissionError:
        return False, "Нет прав: требуется администратор"
    except Exception as e:
        return False, str(e)


def enable_stereo_mix_elevated() -> None:
    """
    Re-launch our own exe with --enable-stereo-mix to trigger a UAC elevation dialog.
    The relaunched process does the registry change and exits silently.
    """
    import ctypes
    exe = sys.executable
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", exe, "--enable-stereo-mix", None, 0  # 0 = SW_HIDE
    )
