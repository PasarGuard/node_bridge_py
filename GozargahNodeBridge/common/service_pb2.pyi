from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class BackendType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    XRAY: _ClassVar[BackendType]
XRAY: BackendType

class Empty(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class BaseInfoResponse(_message.Message):
    __slots__ = ("started", "core_version", "node_version")
    STARTED_FIELD_NUMBER: _ClassVar[int]
    CORE_VERSION_FIELD_NUMBER: _ClassVar[int]
    NODE_VERSION_FIELD_NUMBER: _ClassVar[int]
    started: bool
    core_version: str
    node_version: str
    def __init__(self, started: bool = ..., core_version: _Optional[str] = ..., node_version: _Optional[str] = ...) -> None: ...

class Backend(_message.Message):
    __slots__ = ("type", "config", "users", "keep_alive")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    USERS_FIELD_NUMBER: _ClassVar[int]
    KEEP_ALIVE_FIELD_NUMBER: _ClassVar[int]
    type: BackendType
    config: str
    users: _containers.RepeatedCompositeFieldContainer[User]
    keep_alive: int
    def __init__(self, type: _Optional[_Union[BackendType, str]] = ..., config: _Optional[str] = ..., users: _Optional[_Iterable[_Union[User, _Mapping]]] = ..., keep_alive: _Optional[int] = ...) -> None: ...

class Log(_message.Message):
    __slots__ = ("detail",)
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    detail: str
    def __init__(self, detail: _Optional[str] = ...) -> None: ...

class Stat(_message.Message):
    __slots__ = ("name", "type", "link", "value")
    NAME_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    LINK_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    name: str
    type: str
    link: str
    value: int
    def __init__(self, name: _Optional[str] = ..., type: _Optional[str] = ..., link: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...

class StatResponse(_message.Message):
    __slots__ = ("stats",)
    STATS_FIELD_NUMBER: _ClassVar[int]
    stats: _containers.RepeatedCompositeFieldContainer[Stat]
    def __init__(self, stats: _Optional[_Iterable[_Union[Stat, _Mapping]]] = ...) -> None: ...

class StatRequest(_message.Message):
    __slots__ = ("name", "reset")
    NAME_FIELD_NUMBER: _ClassVar[int]
    RESET_FIELD_NUMBER: _ClassVar[int]
    name: str
    reset: bool
    def __init__(self, name: _Optional[str] = ..., reset: bool = ...) -> None: ...

class OnlineStatResponse(_message.Message):
    __slots__ = ("name", "value")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    name: str
    value: int
    def __init__(self, name: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...

class StatsOnlineIpListResponse(_message.Message):
    __slots__ = ("name", "ips")
    class IpsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    NAME_FIELD_NUMBER: _ClassVar[int]
    IPS_FIELD_NUMBER: _ClassVar[int]
    name: str
    ips: _containers.ScalarMap[str, int]
    def __init__(self, name: _Optional[str] = ..., ips: _Optional[_Mapping[str, int]] = ...) -> None: ...

class BackendStatsResponse(_message.Message):
    __slots__ = ("num_goroutine", "num_gc", "alloc", "total_alloc", "sys", "mallocs", "frees", "live_objects", "pause_total_ns", "uptime")
    NUM_GOROUTINE_FIELD_NUMBER: _ClassVar[int]
    NUM_GC_FIELD_NUMBER: _ClassVar[int]
    ALLOC_FIELD_NUMBER: _ClassVar[int]
    TOTAL_ALLOC_FIELD_NUMBER: _ClassVar[int]
    SYS_FIELD_NUMBER: _ClassVar[int]
    MALLOCS_FIELD_NUMBER: _ClassVar[int]
    FREES_FIELD_NUMBER: _ClassVar[int]
    LIVE_OBJECTS_FIELD_NUMBER: _ClassVar[int]
    PAUSE_TOTAL_NS_FIELD_NUMBER: _ClassVar[int]
    UPTIME_FIELD_NUMBER: _ClassVar[int]
    num_goroutine: int
    num_gc: int
    alloc: int
    total_alloc: int
    sys: int
    mallocs: int
    frees: int
    live_objects: int
    pause_total_ns: int
    uptime: int
    def __init__(self, num_goroutine: _Optional[int] = ..., num_gc: _Optional[int] = ..., alloc: _Optional[int] = ..., total_alloc: _Optional[int] = ..., sys: _Optional[int] = ..., mallocs: _Optional[int] = ..., frees: _Optional[int] = ..., live_objects: _Optional[int] = ..., pause_total_ns: _Optional[int] = ..., uptime: _Optional[int] = ...) -> None: ...

class SystemStatsResponse(_message.Message):
    __slots__ = ("mem_total", "mem_used", "cpu_cores", "cpu_usage", "incoming_bandwidth_speed", "outgoing_bandwidth_speed")
    MEM_TOTAL_FIELD_NUMBER: _ClassVar[int]
    MEM_USED_FIELD_NUMBER: _ClassVar[int]
    CPU_CORES_FIELD_NUMBER: _ClassVar[int]
    CPU_USAGE_FIELD_NUMBER: _ClassVar[int]
    INCOMING_BANDWIDTH_SPEED_FIELD_NUMBER: _ClassVar[int]
    OUTGOING_BANDWIDTH_SPEED_FIELD_NUMBER: _ClassVar[int]
    mem_total: int
    mem_used: int
    cpu_cores: int
    cpu_usage: float
    incoming_bandwidth_speed: int
    outgoing_bandwidth_speed: int
    def __init__(self, mem_total: _Optional[int] = ..., mem_used: _Optional[int] = ..., cpu_cores: _Optional[int] = ..., cpu_usage: _Optional[float] = ..., incoming_bandwidth_speed: _Optional[int] = ..., outgoing_bandwidth_speed: _Optional[int] = ...) -> None: ...

class Vmess(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class Vless(_message.Message):
    __slots__ = ("id", "flow")
    ID_FIELD_NUMBER: _ClassVar[int]
    FLOW_FIELD_NUMBER: _ClassVar[int]
    id: str
    flow: str
    def __init__(self, id: _Optional[str] = ..., flow: _Optional[str] = ...) -> None: ...

class Trojan(_message.Message):
    __slots__ = ("password",)
    PASSWORD_FIELD_NUMBER: _ClassVar[int]
    password: str
    def __init__(self, password: _Optional[str] = ...) -> None: ...

class Shadowsocks(_message.Message):
    __slots__ = ("password", "method")
    PASSWORD_FIELD_NUMBER: _ClassVar[int]
    METHOD_FIELD_NUMBER: _ClassVar[int]
    password: str
    method: str
    def __init__(self, password: _Optional[str] = ..., method: _Optional[str] = ...) -> None: ...

class Proxy(_message.Message):
    __slots__ = ("vmess", "vless", "trojan", "shadowsocks")
    VMESS_FIELD_NUMBER: _ClassVar[int]
    VLESS_FIELD_NUMBER: _ClassVar[int]
    TROJAN_FIELD_NUMBER: _ClassVar[int]
    SHADOWSOCKS_FIELD_NUMBER: _ClassVar[int]
    vmess: Vmess
    vless: Vless
    trojan: Trojan
    shadowsocks: Shadowsocks
    def __init__(self, vmess: _Optional[_Union[Vmess, _Mapping]] = ..., vless: _Optional[_Union[Vless, _Mapping]] = ..., trojan: _Optional[_Union[Trojan, _Mapping]] = ..., shadowsocks: _Optional[_Union[Shadowsocks, _Mapping]] = ...) -> None: ...

class User(_message.Message):
    __slots__ = ("email", "proxies", "inbounds")
    EMAIL_FIELD_NUMBER: _ClassVar[int]
    PROXIES_FIELD_NUMBER: _ClassVar[int]
    INBOUNDS_FIELD_NUMBER: _ClassVar[int]
    email: str
    proxies: Proxy
    inbounds: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, email: _Optional[str] = ..., proxies: _Optional[_Union[Proxy, _Mapping]] = ..., inbounds: _Optional[_Iterable[str]] = ...) -> None: ...

class Users(_message.Message):
    __slots__ = ("users",)
    USERS_FIELD_NUMBER: _ClassVar[int]
    users: _containers.RepeatedCompositeFieldContainer[User]
    def __init__(self, users: _Optional[_Iterable[_Union[User, _Mapping]]] = ...) -> None: ...
