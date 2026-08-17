"""Describe the machine a result was measured on (macOS and Linux)."""
import os
import platform
import subprocess


def _sysctl(name):
    try:
        out = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _power_source():
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
    except OSError:
        return None
    if "AC Power" in out:
        return "ac"
    if "Battery Power" in out:
        return "battery"
    return None


def describe():
    """A JSON-serialisable summary of CPU topology, caches and memory."""
    info = {
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "logical_cpus": os.cpu_count(),
    }
    if platform.system() == "Darwin":
        info.update({
            "cpu": _sysctl("machdep.cpu.brand_string"),
            "model": _sysctl("hw.model"),
            "memory_bytes": _int(_sysctl("hw.memsize")),
            "page_size": _int(_sysctl("hw.pagesize")),
            "power_source": _power_source(),
            "clusters": [],
        })
        for level in range(int(_sysctl("hw.nperflevels") or 1)):
            p = f"hw.perflevel{level}."
            info["clusters"].append({
                "name": _sysctl(p + "name"),
                "cores": _int(_sysctl(p + "physicalcpu")),
                "l1d_bytes": _int(_sysctl(p + "l1dcachesize")),
                "l2_bytes": _int(_sysctl(p + "l2cachesize")),
                "cpus_per_l2": _int(_sysctl(p + "cpusperl2")),
            })
    elif platform.system() == "Linux":
        cpu = None
        try:
            with open("/proc/cpuinfo") as fh:
                for line in fh:
                    if line.startswith("model name"):
                        cpu = line.split(":", 1)[1].strip()
                        break
        except OSError:
            pass
        info["cpu"] = cpu
        try:
            info["memory_bytes"] = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            info["page_size"] = os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError):
            pass
    return info


if __name__ == "__main__":
    import json
    print(json.dumps(describe(), indent=2))
