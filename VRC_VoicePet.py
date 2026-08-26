import shutil
import os
import sys
import re
import json
import random
import time
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox
import ctypes
from ctypes import wintypes

import speech_recognition as sr
import pykakasi

from vrchat_osc_control import VRChatOSC


# =========================================================
# 基本設定
# =========================================================

APP_TITLE = "VRChat Voice Word Controller"

BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
MEMO_FILE = os.path.join(ASSETS_DIR, "memo.txt")
SETTING_FILE = os.path.join(ASSETS_DIR, "setting.txt")
ICON_FILE = os.path.join(ASSETS_DIR, "icon.ico")
LANGUAGE = "ja-JP"
APP_USER_MODEL_ID = "VRChat.VoiceWordController"
DEFAULT_PRESET_NAMES = ["preset1.txt", "preset2.txt", "preset3.txt"]
PRESET_META_FILE = os.path.join(ASSETS_DIR, "presets.json")

# VRChat Parameters
VRCPARAMETER_COUNT = "TBG_Count"
VRCPARAMETER_VALUE = "TBG_Value"
VRCPARAMETER_RESET = "TBG_Reset"
VRCPARAMETER_ONOFF = "TGB_ON_OFF"
VRCPARAMETER_SPELL = "TGB_Spell_Count"


# =========================================================
# Windows / WASAPI
# =========================================================

CLSID_MMDeviceEnumerator = None
IID_IMMDeviceEnumerator = None
PKEY_Device_FriendlyName = None


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]



def get_preset_files():
    """Return all preset text files stored in the application data directory."""
    base_dir = os.path.dirname(os.path.abspath(MEMO_FILE))
    try:
        names = os.listdir(base_dir)
    except Exception:
        return []

    files = []
    for name in names:
        if not name.lower().endswith(".txt"):
            continue
        if re.fullmatch(r"preset(?:[0-9]+|_[^/\\\\]+)\.txt", name, flags=re.IGNORECASE):
            files.append(os.path.join(base_dir, name))

    return sorted(files, key=lambda path: os.path.basename(path).casefold())


def convert_all_presets():
    """Normalize every registered preset while keeping a backup of the original file."""
    files = get_preset_files()
    if not files:
        messagebox.showinfo("プリセット変換", "プリセットファイルが見つかりません。")
        return

    converted = 0
    total_before = 0
    total_after = 0
    total_removed = 0
    total_duplicates = 0
    details = []

    for path in files:
        name = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                original = [line.rstrip("\r\n") for line in f]

            result = []
            seen = set()
            removed = 0
            duplicates = 0

            for raw in original:
                raw = raw.strip()
                if not raw:
                    removed += 1
                    continue

                try:
                    value = clean_text(raw)
                except Exception:
                    value = raw

                value = value.strip()
                if not value:
                    removed += 1
                    continue

                if value in seen:
                    duplicates += 1
                    continue

                seen.add(value)
                result.append(value)

            backup = path + ".bak"
            shutil.copy2(path, backup)

            with open(path, "w", encoding="utf-8", newline="\n") as f:
                if result:
                    f.write("\n".join(result) + "\n")

            converted += 1
            total_before += len(original)
            total_after += len(result)
            total_removed += removed
            total_duplicates += duplicates
            details.append(f"{name}: {len(original)} → {len(result)}行")

            try:
                1
            except Exception:
                pass

        except Exception as e:
            details.append(f"{name}: ERROR - {e}")

    messagebox.showinfo(
        "プリセット変換完了",
        f"{converted}個のプリセットを変換しました。\n"
        f"総行数: {total_before} → {total_after}\n"
        f"空行/変換不能削除: {total_removed}\n"
        f"重複削除: {total_duplicates}\n\n"
        + "\n".join(details)
        + "\n\n元ファイルは .bak に保存されています。"
    )

def make_guid(data1, data2, data3, data4):
    return GUID(
        data1,
        data2,
        data3,
        (wintypes.BYTE * 8)(*data4)
    )


CLSID_MMDeviceEnumerator = make_guid(
    0xBCDE0395,
    0xE52F,
    0x467C,
    [0x8E, 0x3D, 0xC4, 0x57, 0x92, 0x91, 0x69, 0x2E]
)

IID_IMMDeviceEnumerator = make_guid(
    0xA95664D2,
    0x9614,
    0x4F35,
    [0xA7, 0x46, 0xDE, 0x8D, 0xB6, 0x36, 0x17, 0xE6]
)

PKEY_Device_FriendlyName_GUID = make_guid(
    0xA45C254E,
    0xDF1C,
    0x4EFD,
    [0x80, 0x20, 0x67, 0xD1, 0x46, 0xA8, 0x50, 0xE0]
)


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [
        ("fmtid", GUID),
        ("pid", wintypes.DWORD),
    ]


PKEY_Device_FriendlyName = PROPERTYKEY(
    PKEY_Device_FriendlyName_GUID,
    14
)


class PROPVARIANT_UNION(ctypes.Union):
    _fields_ = [
        ("pwszVal", ctypes.c_wchar_p),
        ("data", ctypes.c_byte * 16),
    ]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", wintypes.USHORT),
        ("wReserved1", wintypes.USHORT),
        ("wReserved2", wintypes.USHORT),
        ("wReserved3", wintypes.USHORT),
        ("u", PROPVARIANT_UNION),
    ]


VT_LPWSTR = 31

# eCapture
E_CAPTURE = 1

# DEVICE_STATE_ACTIVE
DEVICE_STATE_ACTIVE = 1

CLSCTX_ALL = 0x17


# =========================================================
# COM
# =========================================================

def _release_com_object(ptr):
    if not ptr:
        return

    try:
        vtable = ctypes.cast(
            ptr,
            ctypes.POINTER(
                ctypes.POINTER(ctypes.c_void_p)
            )
        ).contents

        Release = ctypes.WINFUNCTYPE(
            wintypes.ULONG,
            ctypes.c_void_p
        )(vtable[2])

        Release(ptr)

    except Exception:
        pass


def _get_immdevice_id(device_ptr):
    """
    IMMDevice* -> Windows Endpoint ID
    """

    if not device_ptr:
        return ""

    try:
        vtable = ctypes.cast(
            device_ptr,
            ctypes.POINTER(
                ctypes.POINTER(ctypes.c_void_p)
            )
        ).contents

        GetId = ctypes.WINFUNCTYPE(
            wintypes.LONG,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        )(vtable[5])

        value = ctypes.c_wchar_p()

        hr = GetId(
            device_ptr,
            ctypes.byref(value)
        )

        if hr != 0 or not value:
            return ""

        result = value.value or ""

        try:
            ctypes.windll.ole32.CoTaskMemFree(value)
        except Exception:
            pass

        return result

    except Exception:
        return ""


def _get_friendly_name(device_ptr):
    """
    IMMDevice* -> Windows FriendlyName

    PyAudioの名前は使用しない。
    """

    if not device_ptr:
        return ""

    store = ctypes.c_void_p()

    try:
        vtable = ctypes.cast(
            device_ptr,
            ctypes.POINTER(
                ctypes.POINTER(ctypes.c_void_p)
            )
        ).contents

        OpenPropertyStore = ctypes.WINFUNCTYPE(
            wintypes.LONG,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        )(vtable[4])

        hr = OpenPropertyStore(
            device_ptr,
            0,
            ctypes.byref(store)
        )

        if hr != 0 or not store:
            return ""

        sv = ctypes.cast(
            store,
            ctypes.POINTER(
                ctypes.POINTER(ctypes.c_void_p)
            )
        ).contents

        GetValue = ctypes.WINFUNCTYPE(
            wintypes.LONG,
            ctypes.c_void_p,
            ctypes.POINTER(PROPERTYKEY),
            ctypes.POINTER(PROPVARIANT),
        )(sv[5])

        pv = PROPVARIANT()

        hr = GetValue(
            store,
            ctypes.byref(PKEY_Device_FriendlyName),
            ctypes.byref(pv)
        )

        if (
            hr == 0
            and pv.vt == VT_LPWSTR
            and pv.u.pwszVal
        ):
            return pv.u.pwszVal

        return ""

    finally:
        _release_com_object(store)


# =========================================================
# Windowsの入力デバイス一覧
# =========================================================
def get_windows_active_capture_devices():
    """
    Windowsが現在ACTIVEとして認識している
    音声入力Endpointを取得する。

    ここでは特定メーカー・特定デバイス名には依存しない。

    Discord / LINE / Teams のように、
    現在Windowsで入力デバイスとして利用可能なものを
    広く取得する。

    表示名はWindowsのFriendlyNameを使用するため、
    PyAudio側の文字化けを表示しない。
    """

    if os.name != "nt":
        return []

    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)

    enumerator = ctypes.c_void_p()
    collection = ctypes.c_void_p()

    result = []

    try:
        ole32.CoCreateInstance.restype = wintypes.LONG

        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_MMDeviceEnumerator),
            None,
            CLSCTX_ALL,
            ctypes.byref(IID_IMMDeviceEnumerator),
            ctypes.byref(enumerator),
        )

        if hr != 0 or not enumerator:
            raise RuntimeError(
                f"CoCreateInstance failed: "
                f"0x{hr & 0xFFFFFFFF:08X}"
            )

        ev = ctypes.cast(
            enumerator,
            ctypes.POINTER(
                ctypes.POINTER(ctypes.c_void_p)
            )
        ).contents

        EnumAudioEndpoints = ctypes.WINFUNCTYPE(
            wintypes.LONG,
            ctypes.c_void_p,
            wintypes.INT,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        )(ev[3])

        hr = EnumAudioEndpoints(
            enumerator,
            E_CAPTURE,
            DEVICE_STATE_ACTIVE,
            ctypes.byref(collection),
        )

        if hr != 0 or not collection:
            raise RuntimeError(
                f"EnumAudioEndpoints failed: "
                f"0x{hr & 0xFFFFFFFF:08X}"
            )

        cv = ctypes.cast(
            collection,
            ctypes.POINTER(
                ctypes.POINTER(ctypes.c_void_p)
            )
        ).contents

        GetCount = ctypes.WINFUNCTYPE(
            wintypes.LONG,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.UINT),
        )(cv[3])

        Item = ctypes.WINFUNCTYPE(
            wintypes.LONG,
            ctypes.c_void_p,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
        )(cv[4])

        count = wintypes.UINT()

        hr = GetCount(
            collection,
            ctypes.byref(count)
        )

        if hr != 0:
            raise RuntimeError(
                f"GetCount failed: "
                f"0x{hr & 0xFFFFFFFF:08X}"
            )

        for i in range(count.value):

            device = ctypes.c_void_p()

            if (
                Item(
                    collection,
                    i,
                    ctypes.byref(device)
                ) != 0
                or not device
            ):
                continue

            try:

                endpoint_id = _get_immdevice_id(
                    device
                )

                name = _get_friendly_name(
                    device
                )

                if not endpoint_id or not name:
                    continue

                result.append({
                    "id": endpoint_id,
                    "name": name,
                })

            finally:
                _release_com_object(device)

        return result

    finally:
        _release_com_object(collection)
        _release_com_object(enumerator)

        try:
            ole32.CoUninitialize()
        except Exception:
            pass


# =========================================================
# PortAudio / WASAPI
# =========================================================

def _load_portaudio_wasapi_function(pa_module):
    """
    PaWasapi_GetIMMDeviceを持っている
    PortAudio DLLを探す。
    """

    candidates = []

    module_path = getattr(
        pa_module,
        "__file__",
        ""
    )

    if module_path:

        candidates.append(module_path)

        base = os.path.dirname(module_path)

        for name in (
            "portaudio.dll",
            "libportaudio-2.dll",
            "libportaudio.dll",
            "portaudio_x64.dll",
            "portaudio64.dll",
        ):
            candidates.append(
                os.path.join(base, name)
            )

    candidates.extend([
        "portaudio.dll",
        "libportaudio-2.dll",
        "libportaudio.dll",
        "portaudio_x64.dll",
        "portaudio64.dll",
    ])

    seen = set()

    for path in candidates:

        if not path:
            continue

        if path in seen:
            continue

        seen.add(path)

        try:
            dll = ctypes.WinDLL(path)

            func = getattr(
                dll,
                "PaWasapi_GetIMMDevice",
                None
            )

            if func is not None:

                func.argtypes = [
                    ctypes.c_int,
                    ctypes.POINTER(
                        ctypes.c_void_p
                    )
                ]

                func.restype = ctypes.c_long

                return func

        except Exception:
            continue

    return None


def _get_portaudio_wasapi_endpoint_id(
    pa_module,
    device_index
):
    """
    PortAudioのWASAPIデバイスから
    Windows IMMDevice Endpoint IDを取得する。
    """

    if os.name != "nt":
        return ""

    try:

        func = _load_portaudio_wasapi_function(
            pa_module
        )

        if func is None:
            return ""

        imm = ctypes.c_void_p()

        err = func(
            int(device_index),
            ctypes.byref(imm)
        )

        if err != 0 or not imm:
            return ""

        try:
            return _get_immdevice_id(imm)

        finally:
            _release_com_object(imm)

    except Exception:
        return ""

def get_pyaudio_wasapi_input_devices():
    """
    PyAudioのWASAPI入力デバイスだけを取得する。

    MME / DirectSound / WDM-KS はここでは取得しない。

    目的は、
        同じデバイスが4種類のHostAPIで重複する問題
    を最初から防ぐこと。
    """

    try:
        import pyaudio
        import pyaudio._portaudio as pa_module

        pa = pyaudio.PyAudio()

    except Exception:
        return []

    result = []

    try:

        wasapi_type = getattr(
            pyaudio,
            "paWASAPI",
            13
        )

        for host_index in range(
            pa.get_host_api_count()
        ):

            try:
                host = pa.get_host_api_info_by_index(
                    host_index
                )
            except Exception:
                continue

            if int(
                host.get("type", -1)
            ) != int(wasapi_type):
                continue

            device_count = int(
                host.get(
                    "deviceCount",
                    0
                )
            )

            for local_index in range(
                device_count
            ):

                try:

                    info = (
                        pa.get_device_info_by_host_api_device_index(
                            host_index,
                            local_index
                        )
                    )

                    input_channels = int(
                        info.get(
                            "maxInputChannels",
                            0
                        )
                    )

                    if input_channels <= 0:
                        continue

                    device_index = int(
                        info["index"]
                    )

                    endpoint_id = (
                        _get_portaudio_wasapi_endpoint_id(
                            pa_module,
                            device_index
                        )
                    )

                    result.append({
                        "index": device_index,
                        "name": str(
                            info.get(
                                "name",
                                ""
                            )
                        ),
                        "endpoint_id": endpoint_id,
                        "maxInputChannels":
                            input_channels,
                    })

                except Exception:
                    continue

    finally:
        pa.terminate()

    return result


# =========================================================
# デバイス名の比較用
# =========================================================

def _normalize_device_name(name):
    """
    デバイス名比較用。

    日本語そのものを判定材料にするのではなく、
    空白や改行などを整理する。
    """

    if not name:
        return ""

    text = str(name)

    text = text.replace(
        "\x00",
        ""
    )

    text = text.replace(
        "\r",
        " "
    )

    text = text.replace(
        "\n",
        " "
    )

    text = text.replace(
        "\t",
        " "
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip().casefold()


def _name_similarity(a, b):
    """
    デバイス名の簡易比較。

    完全一致を要求しない。

    ただしこの処理は最後のfallback用。
    基本はEndpoint IDで対応付ける。
    """

    a = _normalize_device_name(a)
    b = _normalize_device_name(b)

    if not a or not b:
        return 0

    if a == b:
        return 100

    if a in b or b in a:
        return 80

    # 英数字だけ抜き出して比較
    aa = "".join(
        ch for ch in a
        if ch.isalnum()
    )

    bb = "".join(
        ch for ch in b
        if ch.isalnum()
    )

    if not aa or not bb:
        return 0

    if aa == bb:
        return 90

    if aa in bb or bb in aa:
        return 70

    return 0


# =========================================================
# Windows入力デバイス → PyAudio WASAPI index
# =========================================================

def build_windows_microphone_list():
    """
    Windowsのマイク選択画面に近い考え方で
    入力デバイス一覧を作る。

    ポイント:

    1. Windows MMDevice APIを正本にする
    2. ACTIVEなCapture Endpointだけ取得
    3. PyAudioはWASAPIだけ使用
    4. Endpoint IDで完全対応付け
    5. Endpoint IDが取れない環境では名前でfallback
    6. MME / DirectSound / WDM-KSの重複を出さない
    7. 特定のデバイス名には依存しない
    8. 仮想マイクも普通のマイクも対象
    9. 表示名はWindows FriendlyNameを使用
    """

    windows_devices = (
        get_windows_active_capture_devices()
    )

    wasapi_devices = (
        get_pyaudio_wasapi_input_devices()
    )

    if not windows_devices:
        return []

    if not wasapi_devices:
        return []

    result = []

    used_pa_indices = set()

    # =====================================================
    # Endpoint IDによる完全一致
    # =====================================================

    pa_by_endpoint = {}

    for device in wasapi_devices:

        endpoint_id = (
            device.get("endpoint_id")
        )

        if not endpoint_id:
            continue

        pa_by_endpoint.setdefault(
            endpoint_id,
            []
        ).append(device)

    for win_device in windows_devices:

        endpoint_id = win_device["id"]

        candidates = (
            pa_by_endpoint.get(
                endpoint_id,
                []
            )
        )

        selected = None

        for candidate in candidates:

            index = candidate["index"]

            if index in used_pa_indices:
                continue

            if candidate[
                "maxInputChannels"
            ] <= 0:
                continue

            selected = candidate
            break

        if selected:

            used_pa_indices.add(
                selected["index"]
            )

            result.append({
                "name":
                    win_device["name"],

                "device_index":
                    selected["index"],

                "windows_id":
                    win_device["id"],

                "host_api":
                    "Windows WASAPI",

                "max_input_channels":
                    selected[
                        "maxInputChannels"
                    ],

                "match":
                    "Endpoint ID",
            })

    # =====================================================
    # Endpoint IDが取得できなかった環境用
    # =====================================================
    #
    # PortAudioのビルドによっては
    # PaWasapi_GetIMMDevice が使えないことがある。
    #
    # その場合だけWindows名とPyAudio名を比較する。
    #
    # 文字化け対策として、
    # 「マイク」などの日本語ではなく、
    # 英数字部分を中心に比較する。
    # =====================================================

    if len(result) < len(windows_devices):

        def make_compare_key(name):

            if not name:
                return ""

            text = str(name).casefold()

            # Windows側にありがちな
            # 「マイク」「入力」などは比較から外す
            text = re.sub(
                r"(マイク|mic|microphone|input|output)",
                " ",
                text,
                flags=re.IGNORECASE
            )

            # 記号をスペース化
            text = re.sub(
                r"[^a-z0-9]+",
                " ",
                text
            )

            text = re.sub(
                r"\s+",
                " ",
                text
            )

            return text.strip()

        for win_device in windows_devices:

            # すでに登録済みならスキップ
            if any(
                d["windows_id"]
                == win_device["id"]
                for d in result
            ):
                continue

            win_key = make_compare_key(
                win_device["name"]
            )

            if not win_key:
                continue

            best = None
            best_score = 0

            win_tokens = set(
                win_key.split()
            )

            for pa_device in wasapi_devices:

                if pa_device[
                    "index"
                ] in used_pa_indices:
                    continue

                if pa_device[
                    "maxInputChannels"
                ] <= 0:
                    continue

                pa_key = make_compare_key(
                    pa_device["name"]
                )

                if not pa_key:
                    continue

                pa_tokens = set(
                    pa_key.split()
                )

                common = (
                    win_tokens
                    & pa_tokens
                )

                if not common:
                    continue

                score = sum(
                    len(token)
                    for token in common
                )

                if score > best_score:

                    best_score = score
                    best = pa_device

            if best and best_score >= 4:

                used_pa_indices.add(
                    best["index"]
                )

                result.append({
                    "name":
                        win_device["name"],

                    "device_index":
                        best["index"],

                    "windows_id":
                        win_device["id"],

                    "host_api":
                        "Windows WASAPI",

                    "max_input_channels":
                        best[
                            "maxInputChannels"
                        ],

                    "match":
                        "name fallback",
                })

    # =====================================================
    # 不要な「システム集約デバイス」を除外
    # =====================================================
    #
    # システムが提供する抽象化デバイスを除外する。
    #
    # 「仮想マイク」は残す。
    #
    # ただしWindows自身が提供する
    # 「サウンドマッパー」などは除外。
    # =====================================================

    exclude_patterns = [

        # Windowsの抽象化デバイス
        "microsoft sound mapper",

        "プライマリ サウンド キャプチャ",
        "primary sound capture",

        # Stereo Mix
        "ステレオ ミキサー",
        "stereo mix",

        # ライン入力は通常のマイク選択欄では除外する。
        # 必要に応じて除外対象を変更できる。
        "ライン入力",
        "line in",

    ]

    filtered = []

    for device in result:

        name = device[
            "name"
        ].casefold()

        excluded = False

        for pattern in exclude_patterns:

            if pattern.casefold() in name:
                excluded = True
                break

        if excluded:
            continue

        filtered.append(device)

    # =====================================================
    # Windowsの列挙順を維持
    # =====================================================

    result = filtered

    return result


# =========================================================
# デバッグ用
# =========================================================

def debug_all_pyaudio_devices():
    """
    必要な場合だけPyAudio全デバイスをログに出す。

    GUIの候補一覧とは別。
    """

    try:

        import pyaudio

        pa = pyaudio.PyAudio()

        print(
            "========== PyAudio ALL DEVICES =========="
        )

        for i in range(
            pa.get_device_count()
        ):

            try:

                info = (
                    pa.get_device_info_by_index(i)
                )

                print(
                    f"[{i}] "
                    f"name={info.get('name')!r} "
                    f"hostApi={info.get('hostApi')} "
                    f"maxInputChannels="
                    f"{info.get('maxInputChannels')} "
                    f"maxOutputChannels="
                    f"{info.get('maxOutputChannels')}"
                )

            except Exception as e:

                print(
                    f"[{i}] ERROR: {e}"
                )

        print(
            "========================================="
        )

        pa.terminate()

    except Exception as e:

        print(
            f"PyAudio debug error: {e}"
        )


# =========================================================
# 文字テーブル
# =========================================================

SEION = [
    "あ", "い", "う", "え", "お",
    "か", "き", "く", "け", "こ",
    "さ", "し", "す", "せ", "そ",
    "た", "ち", "つ", "て", "と",
    "な", "に", "ぬ", "ね", "の",
    "は", "ひ", "ふ", "へ", "ほ",
    "ま", "み", "む", "め", "も",
    "や", "ゆ", "よ",
    "ら", "り", "る", "れ", "ろ",
    "わ", "を", "ん"
]

DAKUON = [
    "が", "ぎ", "ぐ", "げ", "ご",
    "ざ", "じ", "ず", "ぜ", "ぞ",
    "だ", "ぢ", "づ", "で", "ど",
    "ば", "び", "ぶ", "べ", "ぼ"
]

HANDAKUON = [
    "ぱ", "ぴ", "ぷ", "ぺ", "ぽ"
]

SMALL_TSU = ["っ"]

SMALL_Y = [
    "ゃ", "ゅ", "ょ"
]

SMALL_A = [
    "ぁ", "ぃ", "ぅ", "ぇ", "ぉ"
]

LONG_VOWEL_MARK = ["ー"]

BLANK = ["　"]

ALPHABET = [
    "a", "b", "c", "d", "e",
    "f", "g", "h", "i", "j",
    "k", "l", "m", "n", "o",
    "p", "q", "r", "s", "t",
    "u", "v", "w", "x", "y", "z"
]

NUMBER = [
    "0", "1", "2", "3", "4",
    "5", "6", "7", "8", "9"
]

BLANK2 = [" "]

CHAR_TABLE = {}

number = 1

for chars in [
    SEION,
    DAKUON,
    HANDAKUON,
    SMALL_TSU,
    SMALL_Y,
    SMALL_A,
    LONG_VOWEL_MARK,
    BLANK,
    ALPHABET,
    NUMBER,
    BLANK2
]:

    for char in chars:
        CHAR_TABLE[char] = number
        number += 1


# =========================================================
# 文字変換
# =========================================================

def text_to_numbers(text):

    result = []

    for char in text:

        if char in CHAR_TABLE:
            result.append(
                CHAR_TABLE[char]
            )

        else:

            print(
                f"対応していない文字: {char}"
            )

            return None

    return result


kks = pykakasi.kakasi()


def to_hiragana(text):

    result = kks.convert(text)

    return "".join(
        item["hira"]
        for item in result
    )


def clean_text(text):

    text = to_hiragana(text)

    text = text.lower()

    return re.sub(
        r"[^ぁ-ゖー　a-z0-9]",
        "",
        text
    )


# =========================================================
# GUI
# =========================================================

class App:

    def __init__(self, root):

        self.root = root

        self.root.title(
            APP_TITLE
        )

        self.root.geometry(
            "900x700"
        )

        self.root.minsize(
            760,
            600
        )

        os.makedirs(ASSETS_DIR, exist_ok=True)
        self.ensure_preset_files()

        self.osc = VRChatOSC()

        self.stop_event = threading.Event()

        self.running = False

        self.log_queue = queue.Queue()

        self.memo_lock = threading.RLock()

        self.recognition_thread = None

        self.osc_thread_obj = None

        self.recognizer = sr.Recognizer()

        self.microphone_names = []

        self.microphone_devices = []

        self.build_style()

        self.build_ui()

        self.refresh_microphones()

        # 前回の設定を読み込む
        self.load_settings()
        self.setup_setting_auto_save()

        self.root.after(
            100,
            self.process_log_queue
        )

        self.root.protocol(
            "WM_DELETE_WINDOW",
            self.on_close
        )

        self.log(
            "アプリケーション起動"
        )

        self.log(
            f"文字テーブル: "
            f"{len(CHAR_TABLE)}種類"
        )


    # =====================================================
    # 設定保存 / 読み込み
    # =====================================================

    def save_settings(self):
        """現在の設定をsetting.txtへ保存する。"""
        if getattr(self, "_loading_settings", False):
            return

        try:
            mic = self.mic_var.get() or "Windowsの既定のマイク"
            data = [
                ("mic", mic),
                ("phrase_time", self.phrase_time_var.get()),
                ("random_min", self.random_min_var.get()),
                ("random_max", self.random_max_var.get()),
                ("max_words", self.max_words_var.get()),
                ("display_time", self.display_time_var.get()),
                ("theme", self.theme),
                ("word_source", self.word_source_var.get()),
            ]

            with open(SETTING_FILE, "w", encoding="utf-8") as f:
                for key, value in data:
                    # 改行だけは設定ファイルを壊さないよう除去
                    value = str(value).replace("\r", " ").replace("\n", " ")
                    f.write(f"{key}={value}\n")

        except Exception as e:
            self.log(f"設定保存エラー: {e}")


    def load_settings(self):
        """setting.txtから前回の設定を読み込む。"""
        if not os.path.exists(SETTING_FILE):
            self.log("設定ファイルなし: 現在の初期設定を使用します")
            self.save_settings()
            return

        settings = {}

        try:
            with open(SETTING_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    settings[key.strip()] = value.strip()
        except Exception as e:
            self.log(f"設定読み込みエラー: {e}")
            return

        self._loading_settings = True
        try:
            if "phrase_time" in settings:
                self.phrase_time_var.set(settings["phrase_time"])
            if "random_min" in settings:
                self.random_min_var.set(settings["random_min"])
            if "random_max" in settings:
                self.random_max_var.set(settings["random_max"])
            if "max_words" in settings:
                self.max_words_var.set(settings["max_words"])
            if "display_time" in settings:
                self.display_time_var.set(settings["display_time"])

            saved_source = settings.get("word_source", "音声認識モード")
            # 旧バージョンで保存された設定との互換性を維持する。
            if saved_source == "音声認識（memo.txt）":
                saved_source = "音声認識モード"
            if saved_source in self.get_word_source_options():
                self.word_source_var.set(saved_source)
            else:
                self.word_source_var.set("音声認識モード")
            theme = settings.get("theme", "dark").lower()
            self.theme = "light" if theme == "light" else "dark"
            self.apply_theme()

            # マイクはデバイス一覧を再取得した後に名前で復元する。
            saved_mic = settings.get("mic", "Windowsの既定のマイク")
            if saved_mic in self.microphone_names:
                self.mic_combo.set(saved_mic)
            else:
                self.mic_combo.current(0)
                if saved_mic != "Windowsの既定のマイク":
                    self.log(
                        f"保存されていたマイクが見つからないため、既定のマイクを使用: {saved_mic}"
                    )
        finally:
            self._loading_settings = False

        self.log("前回の設定を読み込みました")
        self.log(f"  マイク: {self.mic_var.get()}")
        self.log(f"  話す秒数: {self.phrase_time_var.get()}秒")
        self.log(f"  OSCランダム待機: {self.random_min_var.get()}～{self.random_max_var.get()}秒")
        self.log(f"  最大単語数: {self.max_words_var.get()}個")
        self.log(f"  文字表示タイム: {self.display_time_var.get()}秒")
        self.log(f"  テーマ: {'ライト' if self.theme == 'light' else 'ダーク'}")
        self.log(f"  単語ソース: {self.word_source_var.get()}")
        self.apply_source_mode()


    def setup_setting_auto_save(self):
        """設定変更時に自動保存する。"""
        self.phrase_time_var.trace_add("write", self._setting_changed)
        self.random_min_var.trace_add("write", self._setting_changed)
        self.random_max_var.trace_add("write", self._setting_changed)
        self.max_words_var.trace_add("write", self._setting_changed)
        self.display_time_var.trace_add("write", self._setting_changed)
        self.word_source_var.trace_add("write", self._word_source_changed)
        self.mic_combo.bind("<<ComboboxSelected>>", self._mic_setting_changed)
        self.word_source_combo.bind("<<ComboboxSelected>>", self._word_source_combo_changed)


    def _setting_changed(self, *_):
        if not getattr(self, "_loading_settings", False):
            self.save_settings()


    def _mic_setting_changed(self, _event=None):
        if not getattr(self, "_loading_settings", False):
            self.save_settings()


    def _word_source_changed(self, *_):
        if not getattr(self, "_loading_settings", False):
            self.save_settings()
            self.apply_source_mode()


    def _word_source_combo_changed(self, _event=None):
        if not getattr(self, "_loading_settings", False):
            self.save_settings()
        self.apply_source_mode()

    # =====================================================
    # UI
    # =====================================================

    # =====================================================
    # UIテーマ
    # =====================================================

    def build_style(self):
        """VRChat風ライト/ダークテーマを初期化する。"""

        self.style = ttk.Style()

        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        # 初期テーマ
        self.theme = "dark"

        self.theme_colors = {
            "dark": {
                "bg": "#101014",
                "surface": "#18181F",
                "surface2": "#202027",
                "input": "#0D0D11",
                "border": "#343440",
                "fg": "#F2F2F5",
                "muted": "#A7A7B3",
                "accent": "#7C5CFF",
                "accent_hover": "#9278FF",
                "accent_pressed": "#6042D8",
                "danger": "#E85B72",
                "text_bg": "#0B0B0E",
                "select": "#4D3AA8",
            },
            "light": {
                "bg": "#F5F5F8",
                "surface": "#FFFFFF",
                "surface2": "#EEEEF3",
                "input": "#FFFFFF",
                "border": "#D4D4DD",
                "fg": "#24242B",
                "muted": "#666675",
                "accent": "#6B4EFF",
                "accent_hover": "#7D65FF",
                "accent_pressed": "#5338D0",
                "danger": "#C93F58",
                "text_bg": "#FFFFFF",
                "select": "#DCD4FF",
            },
        }

        self.apply_theme()

    def apply_theme(self):
        """現在のテーマを全ウィジェットへ適用する。"""

        c = self.theme_colors[self.theme]
        s = self.style

        self.root.configure(bg=c["bg"])

        s.configure(
            ".",
            background=c["bg"],
            foreground=c["fg"],
            font=("Yu Gothic UI", 10),
        )

        s.configure(
            "TFrame",
            background=c["bg"],
        )

        s.configure(
            "TLabel",
            background=c["bg"],
            foreground=c["fg"],
        )

        s.configure(
            "Title.TLabel",
            background=c["bg"],
            foreground=c["fg"],
            font=("Yu Gothic UI", 18, "bold"),
        )

        s.configure(
            "Status.TLabel",
            background=c["bg"],
            foreground=c["accent"],
            font=("Yu Gothic UI", 10, "bold"),
        )

        s.configure(
            "Section.TLabelframe",
            background=c["surface"],
            bordercolor=c["border"],
            relief="solid",
        )

        s.configure(
            "TLabelframe",
            background=c["surface"],
            foreground=c["fg"],
            bordercolor=c["border"],
            relief="solid",
        )

        s.configure(
            "TLabelframe.Label",
            background=c["surface"],
            foreground=c["accent"],
            font=("Yu Gothic UI", 10, "bold"),
        )

        s.configure(
            "TButton",
            background=c["surface2"],
            foreground=c["fg"],
            bordercolor=c["border"],
            padding=(12, 7),
            relief="flat",
        )

        s.map(
            "TButton",
            background=[
                ("pressed", c["accent_pressed"]),
                ("active", c["accent_hover"]),
            ],
            foreground=[
                ("pressed", "#FFFFFF"),
                ("active", "#FFFFFF"),
            ],
        )

        s.configure(
            "Accent.TButton",
            background=c["accent"],
            foreground="#FFFFFF",
            bordercolor=c["accent"],
            padding=(16, 8),
            font=("Yu Gothic UI", 10, "bold"),
        )

        s.map(
            "Accent.TButton",
            background=[
                ("pressed", c["accent_pressed"]),
                ("active", c["accent_hover"]),
            ],
            foreground=[
                ("pressed", "#FFFFFF"),
                ("active", "#FFFFFF"),
            ],
        )

        s.configure(
            "TCombobox",
            fieldbackground=c["input"],
            background=c["surface2"],
            foreground=c["fg"],
            bordercolor=c["border"],
            arrowcolor=c["accent"],
            padding=5,
        )

        s.map(
            "TCombobox",
            fieldbackground=[
                ("readonly", c["input"]),
            ],
            foreground=[
                ("readonly", c["fg"]),
            ],
            selectbackground=[
                ("readonly", c["select"]),
            ],
            selectforeground=[
                ("readonly", c["fg"]),
            ],
        )

        s.configure(
            "TSpinbox",
            fieldbackground=c["input"],
            background=c["surface2"],
            foreground=c["fg"],
            bordercolor=c["border"],
            arrowcolor=c["accent"],
            padding=4,
        )

        s.configure(
            "TScrollbar",
            background=c["surface2"],
            troughcolor=c["bg"],
            bordercolor=c["bg"],
            arrowcolor=c["muted"],
        )

        # Tk Text / Listbox は ttk のテーマ外なので別処理。
        if hasattr(self, "log_text"):
            self.log_text.configure(
                background=c["text_bg"],
                foreground=c["fg"],
                insertbackground=c["fg"],
                selectbackground=c["select"],
                selectforeground=c["fg"],
            )

        if hasattr(self, "word_listbox"):
            self.word_listbox.configure(
                background=c["input"],
                foreground=c["fg"],
                selectbackground=c["select"],
                selectforeground=c["fg"],
                highlightbackground=c["border"],
                highlightcolor=c["accent"],
            )

        if hasattr(self, "theme_button"):
            self.theme_button.configure(
                text=(
                    "☀ ライトモード"
                    if self.theme == "dark"
                    else "🌙 ダークモード"
                )
            )

    def toggle_theme(self):
        """ライト/ダークを切り替える。"""

        self.theme = (
            "light"
            if self.theme == "dark"
            else "dark"
        )

        self.apply_theme()
        self.save_settings()

        self.log(
            f"テーマ変更: "
            f"{'ダーク' if self.theme == 'dark' else 'ライト'}"
        )


    def build_ui(self):

        main = ttk.Frame(
            self.root,
            padding=14
        )

        main.pack(
            fill="both",
            expand=True
        )

        title = ttk.Label(
            main,
            text=APP_TITLE,
            style="Title.TLabel"
        )

        title.pack(
            anchor="w",
            pady=(0, 12)
        )


        # =================================================
        # 設定
        # =================================================

        settings = ttk.LabelFrame(
            main,
            text="設定",
            padding=12
        )

        settings.pack(
            fill="x"
        )


        # 単語ソース

        ttk.Label(
            settings,
            text="単語ソース"
        ).grid(
            row=5, column=0, sticky="w", padx=(0, 8), pady=5
        )

        self.word_source_var = tk.StringVar(value="音声認識モード")
        self.word_source_combo = ttk.Combobox(
            settings, textvariable=self.word_source_var, state="readonly", width=55
        )
        self.word_source_combo.grid(
            row=5, column=1, sticky="ew", pady=5
        )
        self.open_source_button = ttk.Button(
            settings, text="ファイルを開く", command=self.open_active_word_file
        )
        self.open_source_button.grid(
            row=5, column=2, padx=(8, 0), pady=5
        )

        self.preset_manage_button = ttk.Button(
            settings, text="プリセット管理", command=self.manage_presets
        )
        self.preset_manage_button.grid(
            row=5, column=3, padx=(8, 0), pady=5
        )

        self.word_source_combo["values"] = self.get_word_source_options()

        preset_convert_frame = ttk.Frame(settings)
        preset_convert_frame.grid(
            row=6, column=0, columnspan=4,
            sticky="ew", pady=(8, 4)
        )

        ttk.Button(
            preset_convert_frame,
            text="プリセットを一括変換",
            command=convert_all_presets
        ).pack(side="left")

        ttk.Label(
            preset_convert_frame,
            text="登録されているプリセットの単語を整理します"
        ).pack(side="left", padx=(10, 0))


        # マイク

        ttk.Label(
            settings,
            text="マイク"
        ).grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=5
        )


        self.mic_var = tk.StringVar()


        self.mic_combo = ttk.Combobox(
            settings,
            textvariable=self.mic_var,
            state="readonly",
            width=55
        )

        self.mic_combo.grid(
            row=0,
            column=1,
            sticky="ew",
            pady=5
        )


        self.refresh_mic_button = ttk.Button(
            settings,
            text="再取得",
            command=self.refresh_microphones
        )

        self.refresh_mic_button.grid(
            row=0,
            column=2,
            padx=(8, 0),
            pady=5
        )


        # 話す秒数

        ttk.Label(
            settings,
            text="話す秒数"
        ).grid(
            row=1,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=5
        )


        self.phrase_time_var = tk.StringVar(
            value="5"
        )

        self.phrase_time_spin = ttk.Spinbox(
            settings,
            from_=1,
            to=60,
            textvariable=self.phrase_time_var,
            width=10
        )

        self.phrase_time_spin.grid(
            row=1,
            column=1,
            sticky="w",
            pady=5
        )


        ttk.Label(
            settings,
            text="秒（最大録音時間）"
        ).grid(
            row=1,
            column=2,
            sticky="w",
            padx=(8, 0),
            pady=5
        )


        # 最大単語数

        ttk.Label(
            settings,
            text="最大単語数"
        ).grid(
            row=3,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=5
        )


        self.max_words_var = tk.StringVar(
            value="1"
        )

        self.max_words_spin = ttk.Spinbox(
            settings,
            from_=1,
            to=5,
            increment=1,
            textvariable=self.max_words_var,
            width=10
        )

        self.max_words_spin.grid(
            row=3,
            column=1,
            sticky="w",
            pady=5
        )

        ttk.Label(
            settings,
            text="単語（1～5個）"
        ).grid(
            row=3,
            column=2,
            sticky="w",
            padx=(8, 0),
            pady=5
        )


        # ランダム待機

        ttk.Label(
            settings,
            text="OSCランダム待機"
        ).grid(
            row=2,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=5
        )


        random_frame = ttk.Frame(
            settings
        )

        random_frame.grid(
            row=2,
            column=1,
            columnspan=2,
            sticky="w",
            pady=5
        )


        self.random_min_var = tk.StringVar(
            value="1"
        )

        self.random_max_var = tk.StringVar(
            value="3"
        )


        ttk.Spinbox(
            random_frame,
            from_=0,
            to=3600,
            textvariable=self.random_min_var,
            width=8
        ).pack(
            side="left"
        )


        ttk.Label(
            random_frame,
            text="～"
        ).pack(
            side="left",
            padx=5
        )


        ttk.Spinbox(
            random_frame,
            from_=0,
            to=3600,
            textvariable=self.random_max_var,
            width=8
        ).pack(
            side="left"
        )


        ttk.Label(
            random_frame,
            text="秒"
        ).pack(
            side="left",
            padx=(5, 0)
        )


        # 文字表示タイム

        ttk.Label(
            settings,
            text="文字表示タイム"
        ).grid(
            row=4,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=5
        )


        self.display_time_var = tk.StringVar(
            value="5"
        )


        self.display_time_spin = ttk.Spinbox(
            settings,
            from_=0.1,
            to=3600,
            increment=0.1,
            textvariable=self.display_time_var,
            width=10
        )

        self.display_time_spin.grid(
            row=4,
            column=1,
            sticky="w",
            pady=5
        )


        ttk.Label(
            settings,
            text="秒（文字表示を維持する時間）"
        ).grid(
            row=4,
            column=2,
            sticky="w",
            padx=(8, 0),
            pady=5
        )


        settings.columnconfigure(
            1,
            weight=1
        )


        # =================================================
        # 操作
        # =================================================

        controls = ttk.Frame(
            main
        )

        controls.pack(
            fill="x",
            pady=12
        )


        self.start_button = ttk.Button(
            controls,
            text="▶ 開始",
            command=self.start
        )

        self.start_button.pack(
            side="left",
            ipadx=18,
            ipady=5
        )


        self.stop_button = ttk.Button(
            controls,
            text="■ 停止",
            command=self.stop,
            state="disabled"
        )

        self.stop_button.pack(
            side="left",
            padx=8,
            ipadx=18,
            ipady=5
        )

        # テーマ切替
        self.theme_button = ttk.Button(
            controls,
            text="☀ ライトモード",
            command=self.toggle_theme
        )

        self.theme_button.pack(
            side="left",
            padx=(8, 0),
            ipadx=8,
            ipady=3
        )


        self.status_var = tk.StringVar(
            value="停止中"
        )


        self.status_label = ttk.Label(
            controls,
            textvariable=self.status_var,
            style="Status.TLabel"
        )

        self.status_label.pack(
            side="right"
        )


        # =================================================
        # 現在状態
        # =================================================

        status_frame = ttk.LabelFrame(
            main,
            text="現在の状態",
            padding=10
        )

        status_frame.pack(
            fill="x",
            pady=(0, 10)
        )


        self.last_recognition_var = tk.StringVar(
            value="-"
        )

        self.last_word_var = tk.StringVar(
            value="-"
        )

        self.selected_word_var = tk.StringVar(
            value="-"
        )

        self.word_count_var = tk.StringVar(
            value="0"
        )


        ttk.Label(
            status_frame,
            text="認識結果:"
        ).grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 8)
        )


        ttk.Label(
            status_frame,
            textvariable=self.last_recognition_var
        ).grid(
            row=0,
            column=1,
            sticky="w"
        )


        ttk.Label(
            status_frame,
            text="登録文字列:"
        ).grid(
            row=1,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=3
        )


        ttk.Label(
            status_frame,
            textvariable=self.last_word_var
        ).grid(
            row=1,
            column=1,
            sticky="w"
        )


        ttk.Label(
            status_frame,
            text="OSC選択:"
        ).grid(
            row=0,
            column=2,
            sticky="w",
            padx=(35, 8)
        )


        ttk.Label(
            status_frame,
            textvariable=self.selected_word_var
        ).grid(
            row=0,
            column=3,
            sticky="w"
        )


        ttk.Label(
            status_frame,
            text="登録単語数:"
        ).grid(
            row=1,
            column=2,
            sticky="w",
            padx=(35, 8),
            pady=3
        )


        ttk.Label(
            status_frame,
            textvariable=self.word_count_var
        ).grid(
            row=1,
            column=3,
            sticky="w"
        )


        # =================================================
        # Notebook
        # =================================================

        notebook = ttk.Notebook(
            main
        )

        notebook.pack(
            fill="both",
            expand=True
        )


        # =================================================
        # ログ
        # =================================================

        log_page = ttk.Frame(
            notebook,
            padding=8
        )

        notebook.add(
            log_page,
            text="ログ"
        )


        log_inner = ttk.Frame(
            log_page
        )

        log_inner.pack(
            fill="both",
            expand=True
        )


        self.log_text = tk.Text(
            log_inner,
            wrap="word",
            state="disabled",
            font=("Consolas", 10)
        )

        self.log_text.pack(
            side="left",
            fill="both",
            expand=True
        )


        log_scrollbar = ttk.Scrollbar(
            log_inner,
            orient="vertical",
            command=self.log_text.yview
        )

        log_scrollbar.pack(
            side="right",
            fill="y"
        )


        self.log_text.configure(
            yscrollcommand=log_scrollbar.set
        )


        log_bottom = ttk.Frame(
            log_page
        )

        log_bottom.pack(
            fill="x",
            pady=(8, 0)
        )


        ttk.Button(
            log_bottom,
            text="ログ消去",
            command=self.clear_log
        ).pack(
            side="left"
        )


        ttk.Button(
            log_bottom,
            text="memo.txtを開く",
            command=self.open_memo
        ).pack(
            side="left",
            padx=8
        )


        # =================================================
        # 覚えた文字
        # =================================================

        word_page = ttk.Frame(
            notebook,
            padding=8
        )

        notebook.add(
            word_page,
            text="覚えた文字"
        )


        word_top = ttk.Frame(
            word_page
        )

        word_top.pack(
            fill="x",
            pady=(0, 8)
        )


        self.learned_word_count_var = tk.StringVar(
            value="0文字"
        )


        ttk.Label(
            word_top,
            textvariable=self.learned_word_count_var
        ).pack(
            side="left"
        )


        ttk.Button(
            word_top,
            text="一覧を更新",
            command=self.update_word_list
        ).pack(
            side="right"
        )


        ttk.Button(
            word_top,
            text="選択した単語を削除",
            command=self.delete_selected_word
        ).pack(
            side="right",
            padx=(0, 8)
        )


        word_inner = ttk.Frame(
            word_page
        )

        word_inner.pack(
            fill="both",
            expand=True
        )


        self.word_listbox = tk.Listbox(
            word_inner,
            font=("Yu Gothic UI", 12),
            activestyle="none"
        )

        self.word_listbox.pack(
            side="left",
            fill="both",
            expand=True
        )


        word_scrollbar = ttk.Scrollbar(
            word_inner,
            orient="vertical",
            command=self.word_listbox.yview
        )

        word_scrollbar.pack(
            side="right",
            fill="y"
        )


        self.word_listbox.configure(
            yscrollcommand=word_scrollbar.set
        )


        self.update_word_list()

        # 生成済みのウィジェットにもテーマを適用する。
        self.apply_theme()


    # =====================================================
    # 単語ソース / プリセット
    # =====================================================

    def ensure_preset_files(self):
        """Create the default preset files and initialize preset metadata."""
        os.makedirs(ASSETS_DIR, exist_ok=True)

        for name in DEFAULT_PRESET_NAMES:
            path = os.path.join(ASSETS_DIR, name)
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8"):
                    pass

        metadata = self.load_preset_metadata()
        changed = False

        for path in self._get_registered_preset_files():
            filename = os.path.basename(path)
            if filename not in metadata:
                if re.fullmatch(r"preset[0-9]+\.txt", filename, flags=re.IGNORECASE):
                    display_name = os.path.splitext(filename)[0]
                else:
                    display_name = os.path.splitext(filename)[0].removeprefix("preset_")
                metadata[filename] = display_name or "プリセット"
                changed = True

        if changed or not os.path.exists(PRESET_META_FILE):
            self.save_preset_metadata(metadata)

    def _get_registered_preset_files(self):
        """Return preset files without creating any new files."""
        try:
            names = os.listdir(ASSETS_DIR)
        except Exception:
            return []

        result = []
        for name in names:
            if not name.lower().endswith(".txt"):
                continue
            if re.fullmatch(r"preset(?:[0-9]+|_[^/\\\\]+)\.txt", name, flags=re.IGNORECASE):
                result.append(os.path.join(ASSETS_DIR, name))

        return sorted(result, key=lambda path: os.path.basename(path).casefold())

    def load_preset_metadata(self):
        """Load the mapping between preset file names and user-visible names."""
        if not os.path.exists(PRESET_META_FILE):
            return {}

        try:
            with open(PRESET_META_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_preset_metadata(self, metadata):
        """Save preset display names."""
        try:
            with open(PRESET_META_FILE, "w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"プリセット情報の保存に失敗しました: {e}")

    def get_preset_entries(self):
        """Return (display_name, file_path) pairs."""
        self.ensure_preset_files()
        metadata = self.load_preset_metadata()

        entries = []
        used_names = set()

        for path in self._get_registered_preset_files():
            filename = os.path.basename(path)
            display_name = str(metadata.get(filename, os.path.splitext(filename)[0])).strip()
            if not display_name:
                display_name = os.path.splitext(filename)[0]

            # Display names are kept unique so the selected item always maps to one file.
            base_name = display_name
            suffix = 2
            while display_name.casefold() in used_names:
                display_name = f"{base_name} ({suffix})"
                suffix += 1

            used_names.add(display_name.casefold())
            entries.append((display_name, path))

        return entries

    def get_preset_names(self):
        return [name for name, _ in self.get_preset_entries()]

    def get_word_source_options(self):
        return ["音声認識モード"] + self.get_preset_names()

    def get_active_word_file(self):
        source = self.word_source_var.get()
        if source == "音声認識モード" or not source:
            return MEMO_FILE

        for display_name, path in self.get_preset_entries():
            if display_name == source:
                return path

        # A saved preset may have been renamed outside the application.
        return os.path.join(ASSETS_DIR, source + ".txt")

    def _preset_filename_is_valid(self, filename):
        """Validate a preset display name for use as a Windows-safe label."""
        name = filename.strip()
        if not name or name in {".", ".."}:
            return False

        if any(char in name for char in '<>:"/\\|?*'):
            return False

        if name.endswith((" ", ".")):
            return False

        reserved = {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
        if name.upper() in reserved:
            return False

        return True

    def _create_preset_file(self):
        """Create a new preset file and return its path."""
        index = 1
        while True:
            filename = f"preset_{index}.txt"
            path = os.path.join(ASSETS_DIR, filename)
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8"):
                    pass
                return path
            index += 1

    def create_preset(self):
        """Create a preset with a user-defined display name."""
        from tkinter import simpledialog

        name = simpledialog.askstring(
            "新しいプリセット",
            "プリセット名を入力してください。",
            parent=self.root,
        )
        if name is None:
            return

        name = name.strip()
        if not self._preset_filename_is_valid(name):
            messagebox.showerror(
                "プリセット名エラー",
                "使用できない名前です。\\n\\n"
                "Windowsで使用できない文字が含まれています。",
                parent=self.root,
            )
            return

        if any(existing.casefold() == name.casefold() for existing in self.get_preset_names()):
            messagebox.showerror(
                "プリセット名エラー",
                "同じ名前のプリセットがすでに存在します。",
                parent=self.root,
            )
            return

        path = self._create_preset_file()
        metadata = self.load_preset_metadata()
        metadata[os.path.basename(path)] = name
        self.save_preset_metadata(metadata)

        self.refresh_preset_list(name)
        self.log(f"プリセットを作成しました: {name}")

    def rename_preset(self):
        """Rename the selected preset without changing its file contents."""
        from tkinter import simpledialog

        current = self.word_source_var.get()
        if current == "音声認識モード" or not current:
            messagebox.showinfo("プリセット名変更", "プリセットを選択してください。", parent=self.root)
            return

        name = simpledialog.askstring(
            "プリセット名変更",
            "新しいプリセット名を入力してください。",
            initialvalue=current,
            parent=self.root,
        )
        if name is None:
            return

        name = name.strip()
        if not self._preset_filename_is_valid(name):
            messagebox.showerror(
                "プリセット名エラー",
                "使用できない名前です。\\n\\n"
                "Windowsで使用できない文字が含まれています。",
                parent=self.root,
            )
            return

        if any(
            existing.casefold() == name.casefold() and existing != current
            for existing in self.get_preset_names()
        ):
            messagebox.showerror(
                "プリセット名エラー",
                "同じ名前のプリセットがすでに存在します。",
                parent=self.root,
            )
            return

        path = self.get_active_word_file()
        filename = os.path.basename(path)
        metadata = self.load_preset_metadata()
        metadata[filename] = name
        self.save_preset_metadata(metadata)

        self.refresh_preset_list(name)
        self.log(f"プリセット名を変更しました: {current} → {name}")

    def delete_preset(self):
        """Delete the selected preset after confirmation."""
        current = self.word_source_var.get()
        if current == "音声認識モード" or not current:
            messagebox.showinfo("プリセット削除", "プリセットを選択してください。", parent=self.root)
            return

        if not messagebox.askyesno(
            "プリセット削除",
            f"「{current}」を削除しますか？\\n\\nこの操作は元に戻せません。",
            parent=self.root,
        ):
            return

        path = self.get_active_word_file()
        try:
            if os.path.exists(path):
                os.remove(path)

            metadata = self.load_preset_metadata()
            metadata.pop(os.path.basename(path), None)
            self.save_preset_metadata(metadata)

            self.word_source_var.set("音声認識モード")
            self.refresh_preset_list("音声認識モード")
            self.log(f"プリセットを削除しました: {current}")
        except Exception as e:
            messagebox.showerror("プリセット削除エラー", str(e), parent=self.root)

    def refresh_preset_list(self, selected=None):
        """Refresh the preset combo box and keep the requested selection."""
        values = self.get_word_source_options()
        self.word_source_combo["values"] = values

        if selected in values:
            self.word_source_var.set(selected)
        elif self.word_source_var.get() not in values:
            self.word_source_var.set("音声認識モード")

        self.apply_source_mode()

    def manage_presets(self):
        """Open the preset management dialog."""
        dialog = tk.Toplevel(self.root)
        dialog.title("プリセット管理")
        dialog.geometry("520x360")
        dialog.minsize(420, 300)
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="プリセット管理",
            style="Title.TLabel"
        ).pack(anchor="w", pady=(0, 8))

        ttk.Label(
            frame,
            text="名前を付けて複数の単語リストを切り替えられます。"
        ).pack(anchor="w", pady=(0, 8))

        listbox = tk.Listbox(frame, font=("Yu Gothic UI", 11), activestyle="none")
        listbox.pack(fill="both", expand=True)

        def refresh():
            listbox.delete(0, tk.END)
            for name in self.get_preset_names():
                listbox.insert(tk.END, name)

        def select_current():
            current = self.word_source_var.get()
            for i, name in enumerate(self.get_preset_names()):
                if name == current:
                    listbox.selection_set(i)
                    listbox.see(i)
                    break

        def choose():
            selected = listbox.curselection()
            if not selected:
                return
            name = listbox.get(selected[0])
            self.word_source_var.set(name)
            self.apply_source_mode()
            self.save_settings()
            dialog.destroy()

        def create():
            self.create_preset()
            refresh()
            select_current()

        def rename():
            selected = listbox.curselection()
            if not selected:
                messagebox.showinfo("プリセット名変更", "変更するプリセットを選択してください。", parent=dialog)
                return
            name = listbox.get(selected[0])
            self.word_source_var.set(name)
            self.rename_preset()
            refresh()
            select_current()

        def delete():
            selected = listbox.curselection()
            if not selected:
                messagebox.showinfo("プリセット削除", "削除するプリセットを選択してください。", parent=dialog)
                return
            name = listbox.get(selected[0])
            self.word_source_var.set(name)
            self.delete_preset()
            refresh()
            select_current()

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))

        ttk.Button(buttons, text="新規作成", command=create).pack(side="left")
        ttk.Button(buttons, text="名前変更", command=rename).pack(side="left", padx=6)
        ttk.Button(buttons, text="削除", command=delete).pack(side="left")
        ttk.Button(buttons, text="選択", command=choose).pack(side="right")
        ttk.Button(buttons, text="閉じる", command=dialog.destroy).pack(side="right", padx=6)

        refresh()
        select_current()
        self.apply_theme()

    def read_word_file(self, path):
        with self.memo_lock:
            if not os.path.exists(path):
                return []
            with open(path, "r", encoding="utf-8") as f:
                return [line.strip() for line in f if line.strip()]

    def read_active_words(self):
        return self.read_word_file(self.get_active_word_file())

    def apply_source_mode(self):
        if not hasattr(self, "word_source_combo"):
            return

        source = self.word_source_var.get()
        voice_mode = source == "音声認識モード"

        self.word_source_combo["values"] = self.get_word_source_options()

        if voice_mode:
            self.mic_combo.configure(state="readonly")
            self.refresh_mic_button.configure(state="normal")
            self.phrase_time_spin.configure(state="normal")
            self.last_recognition_var.set("-")
        else:
            self.mic_combo.configure(state="disabled")
            self.refresh_mic_button.configure(state="disabled")
            self.phrase_time_spin.configure(state="disabled")
            self.last_recognition_var.set("プリセットモード（音声認識なし）")

        try:
            self.word_count_var.set(str(len(self.read_active_words())))
        except Exception:
            self.word_count_var.set("0")

    # =====================================================
    # マイク
    # =====================================================

    def refresh_microphones(self):
        """
        Windowsで認識されている入力デバイスを取得する。

        先頭に「Windowsの既定のマイク」を追加する。

        Windowsの既定マイクを選択した場合は
        speech_recognition.Microphone() に任せる。

        個別デバイスを選択した場合は、
        Windows MMDevice APIと対応付けた
        PyAudio WASAPI device indexを使用する。
        """

        try:
            devices = build_windows_microphone_list()

            self.microphone_devices = devices

            # -------------------------------------------------
            # 先頭に「Windowsの既定のマイク」を追加
            # -------------------------------------------------

            display_names = [
                "Windowsの既定のマイク"
            ]

            display_names.extend(
                d["name"]
                for d in devices
            )

            self.microphone_names = display_names

            self.mic_combo["values"] = (
                self.microphone_names
            )

            # -------------------------------------------------
            # 現在の選択を維持
            # -------------------------------------------------

            current = self.mic_var.get()

            if current in self.microphone_names:
                self.mic_combo.set(current)
            else:
                # デフォルトを選択
                self.mic_combo.current(0)

            # -------------------------------------------------
            # ログ
            # -------------------------------------------------

            self.log(
                f"Windows認識済み入力デバイス: "
                f"{len(devices)}個"
            )

            self.log(
                "  [0] Windowsの既定のマイク "
                "(Windows既定デバイス)"
            )

            for i, d in enumerate(
                devices,
                start=1
            ):
                self.log(
                    f"  [{i}] {d['name']} "
                    f"(PyAudio index={d['device_index']}, "
                    f"match={d['match']})"
                )

        except Exception as e:

            self.log(
                f"マイク取得エラー: {e}"
            )

            self.microphone_devices = []

            self.microphone_names = [
                "Windowsの既定のマイク"
            ]

            self.mic_combo["values"] = (
                self.microphone_names
            )

            self.mic_combo.current(0)

            messagebox.showerror(
                "マイク取得エラー",
                str(e)
            )


    def get_selected_mic_index(self):
        """GUIで選択したマイクのPyAudio indexを返す。"""

        index = self.mic_combo.current()

        if index < 0:
            raise RuntimeError("マイクを選択してください")

        # -------------------------------------------------
        # 0番 = Windowsの既定のマイク
        # -------------------------------------------------

        if index == 0:
            return None

        # -------------------------------------------------
        # 1番以降 = build_windows_microphone_list()
        # のデバイス
        # -------------------------------------------------

        device_index = index - 1

        if device_index >= len(self.microphone_devices):
            raise RuntimeError("選択したマイクが見つかりません")

        return self.microphone_devices[
            device_index
        ]["device_index"]


    # =====================================================
    # Log
    # =====================================================

    def log(self, message):

        timestamp = time.strftime(
            "%H:%M:%S"
        )

        self.log_queue.put(
            f"[{timestamp}] {message}"
        )


    def process_log_queue(self):

        try:

            while True:

                message = (
                    self.log_queue.get_nowait()
                )

                self.log_text.configure(
                    state="normal"
                )

                self.log_text.insert(
                    "end",
                    message + "\n"
                )

                self.log_text.see(
                    "end"
                )

                self.log_text.configure(
                    state="disabled"
                )

        except queue.Empty:
            pass

        self.root.after(
            100,
            self.process_log_queue
        )


    def clear_log(self):

        self.log_text.configure(
            state="normal"
        )

        self.log_text.delete(
            "1.0",
            "end"
        )

        self.log_text.configure(
            state="disabled"
        )


    # =====================================================
    # Memo
    # =====================================================

    def read_memo(self):
        return self.read_word_file(MEMO_FILE)


    def add_word(self, word):

        if not word:
            return False

        with self.memo_lock:

            if os.path.exists(
                MEMO_FILE
            ):

                with open(
                    MEMO_FILE,
                    "r",
                    encoding="utf-8"
                ) as f:

                    words = [
                        line.strip()
                        for line in f
                        if line.strip()
                    ]

            else:
                words = []


            if word in words:

                self.log(
                    f"既に登録済み: {word}"
                )

                return False


            with open(
                MEMO_FILE,
                "a",
                encoding="utf-8"
            ) as f:

                f.write(
                    word + "\n"
                )


        self.log(
            f"新しい単語を追加: {word}"
        )


        self.root.after(
            0,
            lambda:
                self.last_word_var.set(
                    word
                )
        )


        self.update_word_count()

        self.root.after(
            0,
            self.update_word_list
        )

        return True


    def update_word_count(self):

        try:

            count = len(
                self.read_active_words()
            )

            self.root.after(
                0,
                lambda:
                    self.word_count_var.set(
                        str(count)
                    )
            )

        except Exception:
            pass


    def update_word_list(self):

        try:

            words = self.read_memo()

            self.word_listbox.delete(
                0,
                "end"
            )

            for index, word in enumerate(
                words,
                start=1
            ):

                self.word_listbox.insert(
                    "end",
                    f"{index:4d}. {word}"
                )

            self.learned_word_count_var.set(
                f"{len(words)}文字"
            )

        except Exception as e:

            self.log(
                f"文字一覧更新エラー: {e}"
            )


    def delete_selected_word(self):

        selection = (
            self.word_listbox.curselection()
        )

        if not selection:

            messagebox.showwarning(
                "単語の削除",
                "削除する単語を一覧から選択してください。"
            )

            return


        index = selection[0]

        words = self.read_memo()

        if index >= len(words):

            self.update_word_list()

            return


        word = words[index]


        if not messagebox.askyesno(
            "単語の削除",
            f"「{word}」を削除しますか？"
        ):
            return


        try:

            with self.memo_lock:

                if os.path.exists(
                    MEMO_FILE
                ):

                    with open(
                        MEMO_FILE,
                        "r",
                        encoding="utf-8"
                    ) as f:

                        current_words = [
                            line.strip()
                            for line in f
                            if line.strip()
                        ]

                else:

                    current_words = []


                if index >= len(
                    current_words
                ):

                    self.root.after(
                        0,
                        self.update_word_list
                    )

                    return


                deleted_word = (
                    current_words.pop(index)
                )


                with open(
                    MEMO_FILE,
                    "w",
                    encoding="utf-8"
                ) as f:

                    for saved_word in current_words:

                        f.write(
                            saved_word + "\n"
                        )


            self.log(
                f"単語を削除: {deleted_word}"
            )


            self.root.after(
                0,
                self.update_word_count
            )

            self.root.after(
                0,
                self.update_word_list
            )

            self.root.after(
                0,
                lambda:
                    self.last_word_var.set("-")
            )


        except Exception as e:

            self.log(
                f"単語削除エラー: {e}"
            )

            messagebox.showerror(
                "単語削除エラー",
                str(e)
            )


    # =====================================================
    # OSC
    # =====================================================

    def numbers_to_osc(
        self,
        osc_num,
        word
    ):

        if not osc_num:
            return

        try:

            self.log(
                f"OSC送信開始: {word}"
            )

            self.osc.set_bool(
                VRCPARAMETER_ONOFF,
                True
            )

            self.osc.set_bool(
                VRCPARAMETER_RESET,
                True
            )

            self.osc.set_float(
                VRCPARAMETER_SPELL,
                len(osc_num) / 40.0
            )

            time.sleep(0.1)

            self.osc.set_bool(
                VRCPARAMETER_RESET,
                False
            )

            time.sleep(0.1)


            for i, num_count in enumerate(
                osc_num,
                start=1
            ):

                if self.stop_event.is_set():
                    break

                self.osc.set_int(
                    VRCPARAMETER_COUNT,
                    i
                )

                self.osc.set_int(
                    VRCPARAMETER_VALUE,
                    num_count
                )

                time.sleep(0.1)


            display_time = float(
                self.display_time_var.get()
            )

            self.log(
                f"文字表示タイム: "
                f"{display_time:.1f}秒"
            )

            self.stop_event.wait(
                display_time
            )


        except Exception as e:

            self.log(
                f"OSC送信エラー: {e}"
            )


        finally:

            try:

                self.osc.set_int(
                    VRCPARAMETER_COUNT,
                    0
                )

                self.osc.set_bool(
                    VRCPARAMETER_ONOFF,
                    False
                )

                self.osc.set_bool(
                    VRCPARAMETER_RESET,
                    True
                )

                time.sleep(0.1)

                self.osc.set_bool(
                    VRCPARAMETER_RESET,
                    False
                )

            except Exception as e:

                self.log(
                    f"OSC終了処理エラー: {e}"
                )

            self.log(
                "OSC送信終了"
            )


    # =====================================================
    # 音声認識
    # =====================================================

    def speech_worker(self):
        try:
            selected_combo_index = self.mic_combo.current()
            mic_index = self.get_selected_mic_index()

            selected_name = self.microphone_names[
                selected_combo_index
            ]

            self.log(
                f"使用マイク: {selected_name}"
            )

            # -------------------------------------------------
            # Windowsの既定のマイク
            # -------------------------------------------------

            if mic_index is None:

                self.log(
                    "Windowsの既定の入力デバイスを使用します"
                )

                microphone = sr.Microphone()

            # -------------------------------------------------
            # 個別に選択したマイク
            # -------------------------------------------------

            else:
                self.log(
                    f"PyAudio device index: {mic_index}"
                )

                microphone = sr.Microphone(
                    device_index=mic_index
                )

            self.log("周囲の音を調整しています...")

            with microphone as source:
                self.recognizer.adjust_for_ambient_noise(
                    source,
                    duration=1
                )

                self.log("音声入力待機中")

                while not self.stop_event.is_set():
                    try:
                        self.log("聞いています...")

                        phrase_time = int(
                            self.phrase_time_var.get()
                        )

                        audio = self.recognizer.listen(
                            source,
                            timeout=None,
                            phrase_time_limit=phrase_time
                        )

                        if self.stop_event.is_set():
                            break

                        self.log("認識中...")

                        text = self.recognizer.recognize_google(
                            audio,
                            language=LANGUAGE
                        )

                        self.root.after(
                            0,
                            lambda t=text:
                            self.last_recognition_var.set(t)
                        )

                        self.log(
                            f"認識結果: {text}"
                        )

                        word = clean_text(text)

                        if not word:
                            self.log(
                                "有効な文字がありません"
                            )
                            continue

                        self.log(
                            f"変換結果: {word}"
                        )

                        self.add_word(word)

                    except sr.UnknownValueError:
                        self.log(
                            "聞き取れませんでした"
                        )

                    except sr.RequestError as e:
                        self.log(
                            f"音声認識サービスエラー: {e}"
                        )
                        self.stop_event.wait(3)

                    except ValueError:
                        self.log(
                            "話す秒数には整数を指定してください"
                        )
                        self.stop_event.wait(1)

                    except Exception as e:
                        self.log(
                            f"音声認識エラー: {e}"
                        )
                        self.stop_event.wait(1)

        except Exception as e:
            self.log(
                f"音声認識スレッド停止: {e}"
            )

    # =====================================================
    # OSCスレッド
    # =====================================================

    def osc_worker(self):

        self.log(
            "OSCランダム送信開始"
        )

        while not self.stop_event.is_set():

            try:

                random_min = float(
                    self.random_min_var.get()
                )

                random_max = float(
                    self.random_max_var.get()
                )

                max_words = int(
                    self.max_words_var.get()
                )

                # 設定値を安全な範囲に収める
                if max_words < 1:
                    max_words = 1
                if max_words > 5:
                    max_words = 5

                if random_min > random_max:
                    random_min, random_max = (
                        random_max,
                        random_min
                    )

                wait_time = random.uniform(
                    random_min,
                    random_max
                )

                self.log(
                    f"次の単語まで "
                    f"{wait_time:.1f} 秒"
                )

                if self.stop_event.wait(wait_time):
                    break

                source_name = self.word_source_var.get()
                words = self.read_active_words()

                if not words:
                    self.log(
                        f"{source_name} に単語がありません"
                    )
                    continue

                # 1～最大単語数をランダム選択
                # 同じ回の中では重複させない
                select_count = random.randint(
                    1,
                    min(max_words, len(words))
                )

                selected_words = random.sample(
                    words,
                    select_count
                )

                self.log(
                    f"ランダム選択: {select_count}個"
                )

                self.log(
                    "選択候補: "
                    + " / ".join(selected_words)
                )

                # 全角スペースで連結。
                # 40文字を超える場合は、
                # 今追加しようとした単語を丸ごと破棄する。
                combined_word = ""

                for selected_word in selected_words:

                    separator = (
                        ""
                        if not combined_word
                        else "　"
                    )

                    candidate = (
                        combined_word
                        + separator
                        + selected_word
                    )

                    if len(candidate) <= 40:
                        combined_word = candidate
                    else:
                        self.log(
                            f"40文字超過のため "
                            f"「{selected_word}」を破棄"
                        )
                        break

                if not combined_word:
                    self.log(
                        "有効な単語を作れませんでした"
                    )
                    continue

                self.root.after(
                    0,
                    lambda w=combined_word:
                        self.selected_word_var.set(w)
                )

                self.log(
                    f"合成された単語: {combined_word}"
                )

                self.log(
                    f"文字数: {len(combined_word)}/40"
                )

                numbers = text_to_numbers(
                    combined_word
                )

                if numbers is None:
                    self.log(
                        "OSC変換できない文字があります: "
                        f"{combined_word}"
                    )
                    continue

                self.numbers_to_osc(
                    numbers,
                    combined_word
                )

            except ValueError:

                if self.stop_event.is_set():
                    break

                self.log(
                    "ランダム待機時間・最大単語数には正しい数値を指定してください"
                )
                self.stop_event.wait(1)

            except Exception as e:

                if self.stop_event.is_set():
                    break

                self.log(
                    f"OSCスレッドエラー: {e}"
                )
                self.stop_event.wait(1)

        self.log(
            "OSCランダム送信停止"
        )


    # =====================================================
    # Start / Stop
    # =====================================================

    def start(self):

        if self.running:
            return


        try:

            voice_mode = self.word_source_var.get() == "音声認識モード"

            if voice_mode:
                self.get_selected_mic_index()

            phrase_time = int(
                self.phrase_time_var.get()
            )

            random_min = float(
                self.random_min_var.get()
            )

            random_max = float(
                self.random_max_var.get()
            )

            display_time = float(
                self.display_time_var.get()
            )

            max_words = int(
                self.max_words_var.get()
            )


            if voice_mode and phrase_time <= 0:

                raise ValueError(
                    "話す秒数は1以上にしてください"
                )


            if (
                random_min < 0
                or random_max < 0
            ):

                raise ValueError(
                    "ランダム待機時間は0以上にしてください"
                )


            if display_time <= 0:

                raise ValueError(
                    "文字表示タイムは0より大きい値にしてください"
                )


            if random_min > random_max:

                raise ValueError(
                    "最小値は最大値以下にしてください"
                )


            if max_words < 1 or max_words > 5:

                raise ValueError(
                    "最大単語数は1～5にしてください"
                )


        except Exception as e:

            messagebox.showerror(
                "設定エラー",
                str(e)
            )

            return


        os.makedirs(ASSETS_DIR, exist_ok=True)
        active_file = self.get_active_word_file()
        if not os.path.exists(active_file):
            with open(active_file, "w", encoding="utf-8"):
                pass

        self.stop_event.clear()

        self.running = True


        self.start_button.configure(
            state="disabled"
        )

        self.stop_button.configure(
            state="normal"
        )

        self.word_source_combo.configure(state="disabled")

        self.status_var.set(
            "● 動作中"
        )


        self.log(
            "================================"
        )

        self.log(
            "プログラム開始"
        )

        self.log(
            f"単語ソース: {self.word_source_var.get()}"
        )

        if voice_mode:
            self.log(
                f"話す秒数: {phrase_time}秒"
            )
        else:
            self.log("音声認識: 無効（プリセットからランダム選択）")

        self.log(
            f"OSC待機: "
            f"{random_min}～{random_max}秒"
        )

        self.log(
            f"最大単語数: {max_words}個"
        )

        self.log(
            f"文字表示タイム: "
            f"{display_time}秒"
        )

        self.log(
            "================================"
        )


        self.update_word_count()


        self.recognition_thread = None

        if voice_mode:
            self.recognition_thread = threading.Thread(
                target=self.speech_worker,
                daemon=True
            )
            self.recognition_thread.start()
        else:
            self.log("プリセットモード: 音声認識スレッドは起動しません")

        self.osc_thread_obj = threading.Thread(
            target=self.osc_worker,
            daemon=True
        )

        self.osc_thread_obj.start()


    def stop(self):

        if not self.running:
            return


        self.log(
            "停止処理を開始..."
        )


        self.stop_event.set()


        try:

            self.osc.set_int(
                VRCPARAMETER_COUNT,
                0
            )

            self.osc.set_bool(
                VRCPARAMETER_ONOFF,
                False
            )

            self.osc.set_bool(
                VRCPARAMETER_RESET,
                True
            )

            self.osc.set_bool(
                VRCPARAMETER_RESET,
                False
            )

        except Exception:
            pass


        self.running = False


        self.start_button.configure(
            state="normal"
        )

        self.stop_button.configure(
            state="disabled"
        )

        self.word_source_combo.configure(state="readonly")
        self.apply_source_mode()

        self.status_var.set(
            "停止中"
        )


        self.log(
            "停止しました"
        )


    # =====================================================
    # その他
    # =====================================================

    def open_active_word_file(self):
        path = self.get_active_word_file()
        os.makedirs(ASSETS_DIR, exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8"):
                pass
        try:
            os.startfile(os.path.abspath(path))
        except Exception as e:
            self.log(f"単語ファイルを開けません: {e}")


    def open_memo(self):

        if not os.path.exists(
            MEMO_FILE
        ):

            with open(
                MEMO_FILE,
                "w",
                encoding="utf-8"
            ):
                pass


        try:

            os.startfile(
                os.path.abspath(
                    MEMO_FILE
                )
            )

        except Exception as e:

            self.log(
                f"memo.txtを開けません: {e}"
            )


    def on_close(self):

        if self.running:
            self.stop()

        self.root.after(
            100,
            self.root.destroy
        )


# =========================================================
# Main
# =========================================================

def set_windows_app_user_model_id():
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )
    except Exception:
        pass


def set_app_icon(root):
    """TkのウィンドウアイコンとWindowsタスクバーのアイコンを設定する。"""
    if not os.path.exists(ICON_FILE):
        return

    try:
        root.iconbitmap(default=ICON_FILE)
        root.iconbitmap(ICON_FILE)
        root.update_idletasks()
    except Exception:
        pass

    if os.name != "nt":
        return

    # iconbitmapだけではタスクバー側に反映されない環境があるため、
    # Win32 APIでWM_SETICONも明示的に送る。
    try:
        user32 = ctypes.windll.user32
        hwnd = root.winfo_id()
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE = 0x00000040
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1

        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]

        hicon = user32.LoadImageW(
            None,
            ICON_FILE,
            IMAGE_ICON,
            0,
            0,
            LR_LOADFROMFILE | LR_DEFAULTSIZE,
        )

        if hicon:
            user32.SendMessageW(
                hwnd, WM_SETICON, ICON_BIG, hicon
            )
            user32.SendMessageW(
                hwnd, WM_SETICON, ICON_SMALL, hicon
            )
    except Exception:
        pass


if __name__ == "__main__":

    set_windows_app_user_model_id()

    root = tk.Tk()
    set_app_icon(root)
    app = App(root)


    root.mainloop()
