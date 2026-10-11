"""Acquire page image(s) from a scanner - silently, using saved settings.

Scanner access is very platform specific, so this module supports the common
backends and always scans with a specific, pre-chosen device and settings
(DPI, color mode, paper source) rather than showing the backend's own
device-chooser UI. Device selection and settings live in the Settings dialog
(legible.settings); "Add page" just scans. Every scan function returns a
list of image file paths (usually one; more than one when scanning from an
Automatic Document Feeder), or an empty list if the backend reports a user
cancel.

Backends:
  * SANE   (Linux / macOS)  via the `sane` package (python-sane)
  * TWAIN  (Windows)        via the `twain` package (pytwain)
  * WIA    (Windows)        via win32com (pywin32); feeder scans of network
                            scanners go over eSCL instead (stdlib HTTP)

WIA is implemented against real hardware (an Epson WF-2860, flatbed + ADF)
and verified end to end, including the exact HRESULT an empty ADF feeder
raises. TWAIN is implemented against its real, inspected Python API but
cannot be exercised on this dev machine (no TWAIN Data Source Manager
installed); SANE is Linux/macOS-only and equally untestable here. Both
follow the same shape as WIA on a best-effort basis and fail safe (fall back
to "flatbed only", never crash) wherever their behavior is unverified.
"""

from __future__ import annotations

import io
import os
import tempfile
from typing import List, Optional, Set, Tuple

from .settings import AppSettings


class ScannerError(Exception):
    """Raised when a scan is attempted but the backend fails."""


def _tmp_image_path(suffix: str = ".png") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="legible_")
    os.close(fd)
    return path


_SOURCE_DISPLAY = {
    "auto": "Automatic",
    "flatbed": "Flatbed",
    "adf": "Automatic Document Feeder",
}
# Safety cap on pages pulled from a feeder in one Add page action, in case a
# driver misbehaves and never reports "empty" (e.g. a duplexer that keeps
# re-feeding). No real document batch should ever hit this.
_ADF_MAX_PAGES = 200


# --------------------------------------------------------------------------
# SANE (Linux / macOS)
# --------------------------------------------------------------------------
_SANE_MODE = {"color": "Color", "gray": "Gray", "bw": "Lineart"}


def _set_sane_option(dev, name: str, value) -> None:
    try:
        setattr(dev, name, value)
    except Exception:
        pass  # driver doesn't support this option; keep its default


def _list_sane_devices() -> List[Tuple[str, str]]:
    import sane  # type: ignore

    sane.init()
    try:
        devices = sane.get_devices()
    except Exception:
        return []
    finally:
        sane.exit()
    # Each entry is (device_name, vendor, model, type) per python-sane.
    out = []
    for d in devices:
        device_id = d[0]
        label = " ".join(part for part in d[1:3] if part) or device_id
        out.append((device_id, label))
    return out


def _sane_source_values(dev):
    """The device's valid values for its "source" option, or None if it
    doesn't expose one (common on flatbed-only scanners)."""
    try:
        return dev.opt["source"].constraint
    except Exception:
        return None


def _sane_supported_source_keys(device_id: str) -> Set[str]:
    """Best-effort - see module docstring. Classifies each valid "source"
    option value by matching against common substrings ("adf"/"feed" vs
    "flat"), since SANE has no fixed vocabulary for this across backends."""
    import sane  # type: ignore

    sane.init()
    try:
        chosen = device_id
        if not chosen:
            devices = sane.get_devices()
            if not devices:
                return {"flatbed"}
            chosen = devices[0][0]
        dev = sane.open(chosen)
        try:
            values = _sane_source_values(dev)
            if not values:
                return {"flatbed"}
            out = set()
            for v in values:
                low = str(v).lower()
                if "adf" in low or "feed" in low:
                    out.add("adf")
                if "flat" in low:
                    out.add("flatbed")
            return out or {"flatbed"}
        finally:
            dev.close()
    except Exception:
        return {"flatbed"}
    finally:
        sane.exit()


def _sane_pick_source_value(dev, source: str) -> Optional[str]:
    values = _sane_source_values(dev)
    if not values:
        return None
    for v in values:
        low = str(v).lower()
        if source == "adf" and ("adf" in low or "feed" in low):
            return v
        if source == "flatbed" and "flat" in low:
            return v
    return None


def _scan_sane(device_id: str, dpi: int, color_mode: str, source: str) -> List[str]:
    import sane  # type: ignore

    sane.init()
    try:
        chosen = device_id
        if not chosen:
            devices = sane.get_devices()
            if not devices:
                raise ScannerError("No SANE scanner found.")
            chosen = devices[0][0]
        dev = sane.open(chosen)
        try:
            _set_sane_option(dev, "resolution", dpi)
            _set_sane_option(dev, "mode", _SANE_MODE.get(color_mode, "Color"))
            if source != "auto":
                value = _sane_pick_source_value(dev, source)
                if value is not None:
                    _set_sane_option(dev, "source", value)

            adf_batch = source == "adf"
            paths: List[str] = []
            for _ in range(_ADF_MAX_PAGES if adf_batch else 1):
                try:
                    pil_image = dev.scan()
                except Exception as exc:  # noqa: BLE001
                    if adf_batch and paths:
                        break  # feeder ran out after >=1 page: batch done
                    if adf_batch:
                        raise ScannerError(
                            "No documents found in the automatic document "
                            "feeder. Load documents and try again."
                        )
                    raise ScannerError(str(exc))
                path = _tmp_image_path(".png")
                pil_image.save(path)
                paths.append(path)
            return paths
        finally:
            dev.close()
    finally:
        sane.exit()


# --------------------------------------------------------------------------
# TWAIN (Windows)
# --------------------------------------------------------------------------
# pytwain's acquire() docs are explicit: "if ds_name is not provided user
# will be presented with selection dialog" - so a concrete device name must
# always be passed to keep this silent. show_ui=False suppresses the source's
# own acquisition UI (brightness/contrast etc.) separately.
_TWAIN_PIXEL_TYPE = {"color": "color", "gray": "gray", "bw": "bw"}


def _list_twain_devices() -> List[Tuple[str, str]]:
    import twain  # type: ignore

    try:
        sm = twain.SourceManager()
    except Exception:
        return []  # no Data Source Manager (twaindsm.dll) installed
    try:
        return [(name, name) for name in (sm.source_list or [])]
    except Exception:
        return []
    finally:
        sm.close()


def _twain_supported_source_keys(device_id: str) -> Set[str]:
    """Best-effort - see module docstring. Probes CAP_FEEDERENABLED via the
    low-level Source API; falls back to "flatbed only" if the DSM/source
    can't be opened or the capability check fails for any reason."""
    import twain  # type: ignore

    try:
        sm = twain.SourceManager()
    except Exception:
        return {"flatbed"}
    try:
        try:
            src = sm.OpenSource(device_id or None)
        except Exception:
            return {"flatbed"}
        try:
            has_feeder = bool(src.GetCapability(twain.CAP_FEEDERENABLED))
        except Exception:
            has_feeder = False
        finally:
            try:
                src.close()
            except Exception:
                pass
        return {"flatbed", "adf"} if has_feeder else {"flatbed"}
    finally:
        sm.close()


def _twain_enable_feeder(device_id: str) -> None:
    """Best-effort: negotiate ADF mode on before looping acquire() calls.
    Silently does nothing if unsupported - see module docstring."""
    import twain  # type: ignore

    try:
        sm = twain.SourceManager()
    except Exception:
        return
    try:
        try:
            src = sm.OpenSource(device_id or None)
        except Exception:
            return
        try:
            src.SetCapability(twain.CAP_FEEDERENABLED, twain.TWTY_BOOL, True)
            src.SetCapability(twain.CAP_AUTOFEED, twain.TWTY_BOOL, True)
        except Exception:
            pass
        finally:
            try:
                src.close()
            except Exception:
                pass
    finally:
        sm.close()


def _scan_twain(device_id: str, dpi: int, color_mode: str, source: str) -> List[str]:
    import twain  # type: ignore

    resolved = device_id
    if not resolved:
        devices = _list_twain_devices()
        if not devices:
            raise ScannerError("No TWAIN scanner found.")
        resolved = devices[0][0]

    adf_batch = source == "adf"
    if adf_batch:
        _twain_enable_feeder(resolved)

    paths: List[str] = []
    for _ in range(_ADF_MAX_PAGES if adf_batch else 1):
        path = _tmp_image_path(".bmp")
        try:
            result = twain.acquire(
                path,
                ds_name=resolved,
                dpi=float(dpi),
                pixel_type=_TWAIN_PIXEL_TYPE.get(color_mode, "color"),
                show_ui=False,
                modal=False,
            )
        except Exception as exc:  # noqa: BLE001
            if adf_batch and paths:
                break  # treat a failure after >=1 page as "feeder empty"
            raise ScannerError(str(exc))
        if result is None:
            break  # cancelled, or (in ADF mode) the feeder ran out
        paths.append(path)
    if adf_batch and not paths:
        raise ScannerError(
            "No documents found in the automatic document feeder (or ADF "
            "mode isn't supported by this driver). Load documents and try "
            "again."
        )
    return paths


# --------------------------------------------------------------------------
# eSCL / AirScan (network scanners) - used under the WIA backend for ADF
# --------------------------------------------------------------------------
# WIA's scripting layer (WIA.DeviceManager / Item.Transfer) is WIA 1.0 API.
# On WIA 2.0 drivers - verified on the Epson WF-2860 with both its WSD and its
# Epson USB driver - a single Transfer() in feeder mode pulls *every* sheet
# through the feeder as one job but hands back only the first page; the next
# Transfer() then fails (busy / unspecified error) and the other pages are
# lost. eSCL, which most network multifunction printers speak, instead
# returns feeder pages one at a time (GET .../NextDocument until 404), so
# whenever the chosen WIA device is a network scanner that also answers
# eSCL, feeder scans are done through it.
_ESCL_NS = {
    "scan": "http://schemas.hp.com/imaging/escl/2011/05/03",
    "pwg": "http://www.pwg.org/schemas/2010/12/sm",
}
_ESCL_COLOR_MODE = {"color": "RGB24", "gray": "Grayscale8", "bw": "Grayscale8"}
# Feeder scans cover the device's maximum length (here 14", US Legal), so
# every A4 page comes back with a blank strip below the sheet; it is cropped
# off afterwards. Asking the scanner for a shorter scan region instead makes
# the WF-2860 stop the feed mid-sheet and report a paper jam.
_A4_HEIGHT_INCHES = 11.69
_ESCL_TIMEOUT = 120  # seconds; NextDocument blocks while a sheet is scanned


def _escl_open(url: str, data: Optional[bytes] = None, method: Optional[str] = None,
               timeout: float = 10):
    import ssl
    import urllib.request

    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "text/xml")
    # Printers serve eSCL over HTTPS with a self-signed certificate, so it
    # can't be verified; this only ever talks to the scanner on the LAN.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def _escl_find_base_url(hosts: List[str]) -> Optional[str]:
    """First "<scheme>://<host>" among `hosts` that answers eSCL, or None."""
    for host in hosts:
        for scheme in ("https", "http"):
            base = f"{scheme}://{host}"
            try:
                with _escl_open(base + "/eSCL/ScannerCapabilities", timeout=5) as resp:
                    if b"ScannerCapabilities" in resp.read():
                        return base
            except Exception:
                continue
    return None


def _escl_capabilities(base: str):
    import xml.etree.ElementTree as ET

    with _escl_open(base + "/eSCL/ScannerCapabilities") as resp:
        return ET.fromstring(resp.read())


def _escl_adf_state(base: str) -> str:
    """e.g. "ScannerAdfLoaded" / "ScannerAdfEmpty", or "" if unreported."""
    import xml.etree.ElementTree as ET

    try:
        with _escl_open(base + "/eSCL/ScannerStatus") as resp:
            root = ET.fromstring(resp.read())
        return root.findtext("scan:AdfState", "", _ESCL_NS)
    except Exception:
        return ""


def _crop_to_a4_height(im, dpi: int):
    """Drop the blank strip below an A4 sheet from a full-length feeder
    scan (see _A4_HEIGHT_INCHES)."""
    height = round(_A4_HEIGHT_INCHES * dpi)
    return im.crop((0, 0, im.width, height)) if im.height > height else im


def _escl_raise_if_jammed(base: str) -> None:
    if _escl_adf_state(base) == "ScannerAdfJam":
        raise ScannerError(
            "Paper jam in the automatic document feeder. Remove the jammed "
            "paper, reload the documents and try again."
        )


def _escl_raise_post_error(exc) -> None:
    if exc.code == 409:  # Conflict: e.g. feeder empty / device busy
        raise ScannerError(
            "The scanner refused the feeder scan (feeder empty or "
            "scanner busy). Load documents and try again."
        )
    raise ScannerError(f"eSCL scan failed (HTTP {exc.code}).")


def _escl_pick_dpi(adf_caps, dpi: int) -> int:
    values = [int(v.text) for v in adf_caps.iterfind(".//scan:DiscreteResolution/scan:XResolution", _ESCL_NS)
              if v.text and v.text.isdigit()]
    return min(values, key=lambda v: abs(v - dpi)) if values else dpi


def _scan_escl_feeder(base: str, dpi: int, color_mode: str) -> List[str]:
    """Scan every sheet in the feeder via eSCL; one PNG path per page."""
    import urllib.error
    import time
    from PIL import Image as PILImage

    caps = _escl_capabilities(base)
    adf_caps = caps.find("scan:Adf/scan:AdfSimplexInputCaps", _ESCL_NS)
    if adf_caps is None:
        raise ScannerError("This scanner has no automatic document feeder.")
    if _escl_adf_state(base) == "ScannerAdfEmpty":
        raise ScannerError(
            "No documents found in the automatic document feeder. Load "
            "documents and try again."
        )
    res = _escl_pick_dpi(adf_caps, dpi)
    settings_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="{_ESCL_NS['scan']}" xmlns:pwg="{_ESCL_NS['pwg']}">
  <pwg:Version>2.0</pwg:Version>
  <pwg:InputSource>Feeder</pwg:InputSource>
  <scan:ColorMode>{_ESCL_COLOR_MODE.get(color_mode, "RGB24")}</scan:ColorMode>
  <scan:XResolution>{res}</scan:XResolution>
  <scan:YResolution>{res}</scan:YResolution>
  <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
  <scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt>
</scan:ScanSettings>"""

    job = ""
    for attempt in range(30):
        try:
            with _escl_open(base + "/eSCL/ScanJobs", data=settings_xml.encode("utf-8"),
                            method="POST") as resp:
                job = resp.headers.get("Location", "")
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 503 and attempt < 29:
                time.sleep(1)  # still finishing a previous job
                continue
            _escl_raise_post_error(exc)
        except OSError as exc:
            raise ScannerError(f"Could not reach the scanner: {exc}")
    if not job:
        raise ScannerError("eSCL scan failed: the scanner returned no job.")
    # Keep only the path: the Location header can name a different scheme
    # or port than the one eSCL actually answers on (the WF-2860 returns an
    # http:// URL to a port where NextDocument just hangs).
    import urllib.parse
    job = base + urllib.parse.urlparse(job).path.rstrip("/")

    paths: List[str] = []
    busy_retries = 0
    finished = False
    try:
        while len(paths) < _ADF_MAX_PAGES:
            try:
                with _escl_open(job + "/NextDocument", timeout=_ESCL_TIMEOUT) as resp:
                    data = resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    finished = True
                    break  # no more pages: feeder is empty
                if exc.code == 503 and busy_retries < 30:
                    busy_retries += 1
                    time.sleep(1)  # page not ready yet
                    continue
                _escl_raise_if_jammed(base)
                if paths:
                    break
                raise ScannerError(f"eSCL scan failed (HTTP {exc.code}).")
            except OSError as exc:
                _escl_raise_if_jammed(base)
                if paths:
                    break
                raise ScannerError(f"Lost connection to the scanner: {exc}")
            busy_retries = 0
            path = _tmp_image_path(".png")
            with PILImage.open(io.BytesIO(data)) as im:
                im = _crop_to_a4_height(im, res)
                if color_mode == "bw":
                    im = im.convert("L").point(lambda v: 255 if v >= 128 else 0, mode="1")
                im.save(path, format="PNG")
            paths.append(path)
    finally:
        if not finished:
            try:  # cancel the job so the scanner isn't left waiting
                _escl_open(job, method="DELETE").close()
            except Exception:
                pass
    if not paths:
        raise ScannerError(
            "No documents found in the automatic document feeder. Load "
            "documents and try again."
        )
    return paths


# --------------------------------------------------------------------------
# WIA (Windows)
# --------------------------------------------------------------------------
# Scans via the low-level DeviceManager/Item/Transfer API rather than
# WIA.CommonDialog.ShowAcquireImage(), which shows its own device-chooser and
# acquisition wizard - exactly the prompt this app now avoids for Add page.
_WIA_INTENT = {"color": 1, "gray": 2, "bw": 4}  # WIA_INTENT_IMAGE_TYPE_*
_WIA_FORMAT_PNG = "{B96B3CAF-0728-11D3-9D7B-0000F81EF32E}"
_WIA_SCANNER_DEVICE_TYPE = 1  # WiaDeviceType.ScannerDeviceType

# WIA_DPS_DOCUMENT_HANDLING_SELECT values and WIA_DPS_DOCUMENT_HANDLING_
# CAPABILITIES bit flags - verified against a real Epson WF-2860 (flatbed +
# ADF): both properties live on the *device* root's Properties, not the scan
# Item's (the Item only exposes Current Intent / Horizontal|Vertical
# Resolution).
_WIA_DOC_HANDLING_FEEDER = 1
_WIA_DOC_HANDLING_FLATBED = 2
_WIA_CAP_FEEDER_BIT = 0x1
_WIA_CAP_FLATBED_BIT = 0x2

# Documented WIA/OLE HRESULTs for common failure cases.
_WIA_CANCEL_HRESULTS = {
    0x80210064,  # user cancelled the acquire dialog (ShowAcquireImage path)
    0x800704C7,  # HRESULT_FROM_WIN32(ERROR_CANCELLED)
    0x80004004,  # E_ABORT
}
_WIA_NO_DEVICE_HRESULTS = {0x80210015}  # WIA_S_NO_DEVICE_AVAILABLE
_WIA_NO_PAPER_HRESULTS = {0x80210003}  # WIA_ERROR_PAPER_EMPTY (ADF empty) -
# confirmed on real hardware, see _com_hresult.

_DISP_E_EXCEPTION = 0x80020009


def _com_hresult(exc) -> int:
    """Return the real underlying HRESULT/SCODE for a pywintypes.com_error.

    WIA driver errors raised through IDispatch (e.g. Item.Transfer()) arrive
    wrapped as DISP_E_EXCEPTION (0x80020009): the top-level hresult just
    means "a scripting exception occurred", and the real, WIA-specific SCODE
    (e.g. WIA_ERROR_PAPER_EMPTY) is nested in the EXCEPINFO struct's last
    field. Confirmed against real hardware: scanning an empty ADF raises
    exactly this shape, with the true SCODE (0x80210003) in excepinfo[5].
    """
    hr = getattr(exc, "hresult", None)
    if hr is None:
        args = getattr(exc, "args", None)
        hr = args[0] if args else 0
    hr = (hr or 0) & 0xFFFFFFFF

    if hr == _DISP_E_EXCEPTION:
        excepinfo = getattr(exc, "excepinfo", None)
        if excepinfo is None:
            args = getattr(exc, "args", None)
            if args and len(args) > 2:
                excepinfo = args[2]
        if excepinfo and len(excepinfo) >= 6 and excepinfo[5]:
            hr = excepinfo[5] & 0xFFFFFFFF
    return hr


def _wia_device_name(info) -> str:
    try:
        return info.Properties("Name").Value
    except Exception:
        return info.DeviceID


def _wia_device_type(info) -> int:
    try:
        return int(info.Type)
    except Exception:
        return -1


def _wia_connect(device_id: str):
    """Connect to a WIA device by id, or the first scanner-type device found
    if empty. Returns (device, item)."""
    import win32com.client  # type: ignore

    manager = win32com.client.Dispatch("WIA.DeviceManager")
    infos = list(manager.DeviceInfos)
    if device_id:
        info = next((i for i in infos if i.DeviceID == device_id), None)
        if info is None:
            raise ScannerError(f"WIA device not found: {device_id}")
    else:
        scanners = [i for i in infos if _wia_device_type(i) == _WIA_SCANNER_DEVICE_TYPE]
        chosen = scanners or infos
        if not chosen:
            raise ScannerError("No WIA scanner is connected.")
        info = chosen[0]
    device = info.Connect()
    item = device.Items[1]  # first scan source
    return device, item


def _list_wia_devices() -> List[Tuple[str, str]]:
    import pythoncom  # type: ignore
    import win32com.client  # type: ignore

    pythoncom.CoInitialize()
    manager = infos = scanners = chosen = None
    try:
        try:
            manager = win32com.client.Dispatch("WIA.DeviceManager")
            infos = list(manager.DeviceInfos)
        except Exception:
            return []
        scanners = [i for i in infos if _wia_device_type(i) == _WIA_SCANNER_DEVICE_TYPE]
        chosen = scanners or infos  # fall back to "everything" if type read fails
        return [(i.DeviceID, _wia_device_name(i)) for i in chosen]
    finally:
        # Drop COM references before tearing down the apartment, or their
        # eventual GC after CoUninitialize() prints spurious "Win32 exception
        # occurred releasing IUnknown" warnings.
        del manager, infos, scanners, chosen
        pythoncom.CoUninitialize()


def _wia_supported_source_keys(device_id: str) -> Set[str]:
    """{'flatbed','adf'} subset this device actually supports, decoded from
    WIA_DPS_DOCUMENT_HANDLING_CAPABILITIES (verified on real hardware). If
    the property is missing entirely (common on simple flatbed-only
    scanners that don't implement automatic document handling at all),
    assume flatbed-only."""
    import pythoncom  # type: ignore

    pythoncom.CoInitialize()
    device = item = None
    try:
        try:
            device, item = _wia_connect(device_id)
        except Exception:
            return {"flatbed"}
        try:
            caps = int(device.Properties("Document Handling Capabilities").Value)
        except Exception:
            return {"flatbed"}
        out = set()
        if caps & _WIA_CAP_FLATBED_BIT:
            out.add("flatbed")
        if caps & _WIA_CAP_FEEDER_BIT:
            out.add("adf")
        return out or {"flatbed"}
    finally:
        del device, item
        pythoncom.CoUninitialize()


_escl_url_cache: dict = {}


def _wia_escl_base_url(device_id: str) -> Optional[str]:
    """eSCL base URL of the network scanner behind a WIA device, or None.

    The host comes from the WIA device's "Presentation URL" (WSD devices
    expose it). Windows' own eSCL WIA device doesn't, so for any device
    whose port contains the printer's UUID, sibling devices with the same UUID
    are checked too. Needs COM initialised by the caller."""
    import re
    import urllib.parse
    import win32com.client  # type: ignore

    if device_id in _escl_url_cache:
        return _escl_url_cache[device_id]
    manager = win32com.client.Dispatch("WIA.DeviceManager")
    infos = list(manager.DeviceInfos)
    if device_id:
        own = [i for i in infos if i.DeviceID == device_id]
    else:
        own = [i for i in infos if _wia_device_type(i) == _WIA_SCANNER_DEVICE_TYPE][:1]

    def prop(info, name):
        try:
            return str(info.Properties(name).Value or "")
        except Exception:
            return ""

    uuid_re = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
    uuids = {u.lower() for i in own for u in uuid_re.findall(prop(i, "Port"))}
    related = own + [
        i for i in infos if i not in own
        and uuids & {u.lower() for u in uuid_re.findall(prop(i, "Port"))}
    ]
    hosts = []
    for info in related:
        # "Presentation URL" is only on the connected device, not DeviceInfo.
        try:
            url = prop(info.Connect(), "Presentation URL")
        except Exception:
            continue
        host = urllib.parse.urlparse(url).hostname
        if host and host not in hosts:
            hosts.append(host)
    base = _escl_find_base_url(hosts) if hosts else None
    _escl_url_cache[device_id] = base
    return base


def _set_wia_document_handling(device, source: str) -> None:
    if source == "auto":
        return  # leave the driver's current/default source untouched
    value = _WIA_DOC_HANDLING_FEEDER if source == "adf" else _WIA_DOC_HANDLING_FLATBED
    try:
        device.Properties("Document Handling Select").Value = value
    except Exception:
        pass  # device doesn't support switching source; proceed with its default


def _wia_transfer_one(item, crop_dpi: Optional[int] = None) -> str:
    """One Transfer() call -> a real PNG path, cropped to A4 height at
    `crop_dpi` if given (feeder scans). Raises pywintypes.com_error on
    failure (e.g. paper empty)."""
    image = item.Transfer(_WIA_FORMAT_PNG)
    raw_path = _tmp_image_path(".wia")
    if os.path.exists(raw_path):
        os.remove(raw_path)  # WIA won't overwrite an existing file
    image.SaveFile(raw_path)
    # Some WIA drivers ignore the requested transfer format (_WIA_FORMAT_PNG
    # above) and always write BMP regardless - confirmed on real hardware.
    # PIL sniffs the real format, so re-save to guarantee the output is
    # actually PNG, matching its extension.
    from PIL import Image as PILImage

    path = _tmp_image_path(".png")
    with open(raw_path, "rb") as fh:
        data = fh.read()
    with PILImage.open(io.BytesIO(data)) as im:
        if crop_dpi:
            im = _crop_to_a4_height(im, crop_dpi)
        im.save(path, format="PNG")
    del image  # release the WIA ImageFile before deleting its backing file
    try:
        os.remove(raw_path)
    except OSError:
        pass  # still locked by the driver/AV scanner; it's only a temp file
    return path


def _scan_wia(device_id: str, dpi: int, color_mode: str, source: str) -> List[str]:
    import pythoncom  # type: ignore
    import pywintypes  # type: ignore

    # WIA is COM; the calling thread must be COM-initialised (STA). No UI is
    # shown, so unlike before this no longer needs to be the main UI thread.
    pythoncom.CoInitialize()
    device = item = None
    try:
        # Feeder scans of network scanners go through eSCL - see the eSCL
        # section above for why WIA can't do them. "Automatic" uses the
        # feeder too whenever paper is loaded in it, like the printer's own
        # scan button does.
        if source in ("adf", "auto"):
            try:
                base = _wia_escl_base_url(device_id)
            except Exception:
                base = None
            if base and (source == "adf" or _escl_adf_state(base) == "ScannerAdfLoaded"):
                return _scan_escl_feeder(base, dpi, color_mode)

        device, item = _wia_connect(device_id)
        _set_wia_document_handling(device, source)

        def set_prop(name, value):
            try:
                item.Properties(name).Value = value
            except Exception:
                pass  # driver doesn't support this property/value

        set_prop("Current Intent", _WIA_INTENT.get(color_mode, 1))
        set_prop("Horizontal Resolution", dpi)
        set_prop("Vertical Resolution", dpi)

        adf_batch = source == "adf"
        paths: List[str] = []
        for _ in range(_ADF_MAX_PAGES if adf_batch else 1):
            try:
                paths.append(_wia_transfer_one(item, dpi if adf_batch else None))
            except pywintypes.com_error as exc:  # type: ignore[attr-defined]
                hr = _com_hresult(exc)
                if hr in _WIA_CANCEL_HRESULTS:
                    break  # user/driver cancelled
                if hr in _WIA_NO_PAPER_HRESULTS:
                    if adf_batch and paths:
                        break  # feeder ran out after >=1 page: batch done
                    if adf_batch:
                        raise ScannerError(
                            "No documents found in the automatic document "
                            "feeder. Load documents and try again."
                        )
                    raise ScannerError("No paper loaded in the scanner/feeder.")
                if hr in _WIA_NO_DEVICE_HRESULTS:
                    raise ScannerError("No WIA scanner is connected.")
                raise ScannerError(f"WIA scan failed (0x{hr:08X}).")
        return paths
    finally:
        # See _list_wia_devices: drop COM references before uninitializing.
        del device, item
        pythoncom.CoUninitialize()


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
# (module_check, backend_name, list_devices_fn, source_keys_fn, scan_fn) in
# preference order.
_BACKENDS = [
    ("sane", "sane", _list_sane_devices, _sane_supported_source_keys, _scan_sane),
    ("twain", "twain", _list_twain_devices, _twain_supported_source_keys, _scan_twain),
    ("win32com", "wia", _list_wia_devices, _wia_supported_source_keys, _scan_wia),
]


def has_hardware_backend() -> bool:
    """True if any scanner backend library (SANE/TWAIN/WIA) can be imported."""
    return bool(list_backends())


def list_backends() -> List[str]:
    """Backend names ("sane"/"twain"/"wia") whose library is importable."""
    import importlib.util

    return [
        name for module, name, _, _, _ in _BACKENDS
        if importlib.util.find_spec(module) is not None
    ]


def list_devices(backend: str) -> List[Tuple[str, str]]:
    """(device_id, display_name) pairs for one backend. Empty list if the
    backend's library isn't installed or enumeration fails (e.g. no driver /
    Data Source Manager present) - callers should treat that as "no devices",
    not an error."""
    for _module, name, list_fn, _sources_fn, _scan_fn in _BACKENDS:
        if name == backend:
            try:
                return list_fn()
            except Exception:
                return []
    return []


def list_sources(backend: str, device_id: str) -> List[Tuple[str, str]]:
    """(source_id, display_name) pairs actually available for one connected
    device - always includes "auto"; "flatbed"/"adf" only when the device is
    reported to support them (WIA: verified via real hardware; TWAIN/SANE:
    best-effort, defaulting to flatbed-only when undetectable)."""
    out = [("auto", _SOURCE_DISPLAY["auto"])]
    for _module, name, _list_fn, sources_fn, _scan_fn in _BACKENDS:
        if name == backend:
            try:
                supported = sources_fn(device_id)
            except Exception:
                supported = {"flatbed"}
            for key in ("flatbed", "adf"):
                if key in supported:
                    out.append((key, _SOURCE_DISPLAY[key]))
            break
    return out


def _resolve_backend(name: str) -> Optional[str]:
    """"auto" -> first backend with an importable library; otherwise the
    named backend if its library is importable, else None."""
    available = list_backends()
    if name == "auto":
        return available[0] if available else None
    return name if name in available else None


def scan_pages(settings: AppSettings) -> List[str]:
    """Scan page(s) using the backend/device/DPI/color-mode/source in
    `settings`. Usually returns a single-item list; scanning with
    scanner_source="adf" returns one item per page pulled from the feeder
    (possibly zero, if the user cancelled before anything transferred).

    Raises ScannerError if no backend/device is available, ADF mode is
    requested but the feeder is empty, or the scan otherwise fails.
    """
    backend = _resolve_backend(settings.scanner_backend)
    if backend is None:
        raise ScannerError(
            "No scanner backend available (SANE/TWAIN/WIA not installed), "
            "or the configured backend isn't installed. Use Import to load "
            "an existing image/PDF instead, or check Settings."
        )
    for _module, name, _list_fn, _sources_fn, scan_fn in _BACKENDS:
        if name == backend:
            return scan_fn(settings.scanner_device, settings.scanner_dpi,
                           settings.scanner_color_mode, settings.scanner_source)
    raise ScannerError(f"Unknown scanner backend: {backend}")
