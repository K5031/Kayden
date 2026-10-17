import os
import threading


_lock = threading.Lock()
_initialized = False


def ensure_audio_ready():
    """Initialize PortAudio through sounddevice once, on the calling thread."""
    global _initialized
    with _lock:
        if not _initialized:
            import sounddevice as sd
            sd.query_devices()
            _initialized = True


def _requested_device(value, environment_name):
    if value is not None:
        return str(value).strip()
    configured = os.environ.get(environment_name)
    return configured.strip() if configured else None


def _select_named_device(devices, requested, channel_key):
    candidates = [
        (index, device)
        for index, device in enumerate(devices)
        if device.get(channel_key, 0) > 0
    ]
    if not requested:
        return None
    try:
        index = int(requested)
    except ValueError:
        lowered = requested.casefold()
        matches = [
            index for index, device in candidates
            if lowered in device.get("name", "").casefold()
        ]
        if matches:
            return matches[0]
    else:
        if any(candidate == index for candidate, _ in candidates):
            return index
    available = ", ".join(
        f"{index}: {device.get('name', 'unknown')}" for index, device in candidates
    ) or "none"
    raise RuntimeError(
        f"Audio device {requested!r} is unavailable. Available devices: {available}"
    )


def resolve_output_device(
    preferred_names=("pipewire", "pulse", "default"),
    device=None,
):
    ensure_audio_ready()
    import sounddevice as sd

    devices = list(sd.query_devices())
    requested = _requested_device(device, "KAYDEN_OUTPUT_DEVICE")
    selected = _select_named_device(devices, requested, "max_output_channels")
    if selected is not None:
        return selected

    candidates = [
        (index, item)
        for index, item in enumerate(devices)
        if item.get("max_output_channels", 0) > 0
    ]
    for preferred in preferred_names:
        preferred = preferred.casefold()
        for index, item in candidates:
            if preferred in item.get("name", "").casefold():
                return index

    default = sd.default.device
    if not isinstance(default, int):
        try:
            default = default[1]
        except (IndexError, KeyError, TypeError):
            default = None
    if isinstance(default, int) and any(index == default for index, _ in candidates):
        return default
    if candidates:
        return candidates[0][0]
    raise RuntimeError(
        "No usable audio output device found. Run `kayden doctor` outside a "
        "container or sandbox and check PipeWire/PulseAudio."
    )


def resolve_pyaudio_input_device(
    preferred_names=("pipewire", "pulse", "default"),
    device=None,
):
    # Whisper uses SpeechRecognition/PyAudio. An unavailable output backend
    # must not prevent microphone input from working.
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        devices = [pa.get_device_info_by_index(i) for i in range(pa.get_device_count())]
        requested = _requested_device(device, "KAYDEN_INPUT_DEVICE")
        selected = _select_named_device(devices, requested, "maxInputChannels")
        if selected is not None:
            return selected

        candidates = [
            (index, item)
            for index, item in enumerate(devices)
            if item.get("maxInputChannels", 0) > 0
        ]
        for preferred in preferred_names:
            preferred = preferred.casefold()
            for index, item in candidates:
                if preferred in item.get("name", "").casefold():
                    return index
        try:
            default = pa.get_default_input_device_info().get("index")
        except (IOError, OSError):
            default = None
        if isinstance(default, int) and any(index == default for index, _ in candidates):
            return default
        if candidates:
            return candidates[0][0]
        raise RuntimeError(
            "No usable microphone found. Run `kayden doctor` outside a container "
            "or sandbox and check PipeWire/PulseAudio."
        )
    finally:
        pa.terminate()


def audio_diagnostics():
    """Return backend/device facts without loading either voice model."""
    result = {"output": [], "input": [], "errors": []}
    try:
        import sounddevice as sd
        result["output"] = [
            f"{index}: {item['name']}"
            for index, item in enumerate(sd.query_devices())
            if item.get("max_output_channels", 0) > 0
        ]
        result["output_default"] = sd.default.device
    except Exception as error:
        result["errors"].append(f"sounddevice: {error}")

    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        try:
            result["input"] = [
                f"{index}: {item['name']}"
                for index in range(pa.get_device_count())
                if (item := pa.get_device_info_by_index(index)).get("maxInputChannels", 0) > 0
            ]
            try:
                result["input_default"] = pa.get_default_input_device_info().get("index")
            except (IOError, OSError):
                result["input_default"] = None
        finally:
            pa.terminate()
    except Exception as error:
        result["errors"].append(f"pyaudio: {error}")
    return result
