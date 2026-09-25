import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


class InvalidURL(ValueError):
    pass


def _normalize_host(host: str) -> str:
    return host.rstrip(".").lower()


def _is_blocked_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    return not ip.is_global or ip.is_multicast or ip.is_unspecified


def _resolve_addresses(host: str, port: int) -> set[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise InvalidURL("Không thể xác thực tên miền") from exc
    addresses = {str(info[4][0]) for info in infos}
    if not addresses:
        raise InvalidURL("Không thể phân giải tên miền")
    if any(_is_blocked_ip(address) for address in addresses):
        raise InvalidURL("URL trỏ tới mạng nội bộ hoặc mạng bị hạn chế")
    return addresses


def get_url_host(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError as exc:
        raise InvalidURL("URL không hợp lệ") from exc
    return _normalize_host(host)


def host_matches(host: str, domains: tuple[str, ...] | list[str]) -> bool:
    normalized = _normalize_host(host)
    for domain in domains:
        candidate = _normalize_host(domain).lstrip(".")
        if normalized == candidate or normalized.endswith("." + candidate):
            return True
    return False


def validate_public_url(
    url: str,
    allowed_hosts: tuple[str, ...] | list[str] | None = None,
    resolve_dns: bool = True,
    allow_private: bool = False,
) -> str:
    if not isinstance(url, str) or not url.strip():
        raise InvalidURL("URL không được để trống")
    value = url.strip()
    if len(value) > 4096:
        raise InvalidURL("URL quá dài")
    try:
        parsed = urlsplit(value)
        host = _normalize_host(parsed.hostname or "")
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise InvalidURL("URL không hợp lệ") from exc
    if parsed.scheme.lower() not in ("http", "https"):
        raise InvalidURL("Chỉ hỗ trợ URL http hoặc https")
    if not host or parsed.username or parsed.password:
        raise InvalidURL("URL không hợp lệ")
    if port is not None and port not in (80, 443):
        raise InvalidURL("Cổng URL không được phép")
    if not allow_private:
        if host in ("localhost", "localhost.localdomain") or host.endswith((".localhost", ".local")):
            raise InvalidURL("Không được truy cập mạng nội bộ")
        try:
            ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            pass
        else:
            if _is_blocked_ip(host):
                raise InvalidURL("URL trỏ tới mạng nội bộ hoặc mạng bị hạn chế")
    if allowed_hosts and not host_matches(host, allowed_hosts):
        raise InvalidURL("Nền tảng không được hỗ trợ")
    if resolve_dns and not allow_private:
        _resolve_addresses(host, port or (443 if parsed.scheme.lower() == "https" else 80))
    return value


def redact_url(url: str, max_length: int = 160) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
        safe = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except (TypeError, ValueError):
        return "[invalid-url]"
    return safe[:max_length]
