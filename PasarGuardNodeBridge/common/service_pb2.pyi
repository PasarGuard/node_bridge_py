from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class BackendType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    XRAY: _ClassVar[BackendType]
    WIREGUARD: _ClassVar[BackendType]

class StatType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    Outbounds: _ClassVar[StatType]
    Outbound: _ClassVar[StatType]
    Inbounds: _ClassVar[StatType]
    Inbound: _ClassVar[StatType]
    UsersStat: _ClassVar[StatType]
    UserStat: _ClassVar[StatType]
XRAY: BackendType
WIREGUARD: BackendType
Outbounds: StatType
Outbound: StatType
Inbounds: StatType
Inbound: StatType
UsersStat: StatType
UserStat: StatType

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
    __slots__ = ("type", "config", "users", "keep_alive", "exclude_inbounds")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    USERS_FIELD_NUMBER: _ClassVar[int]
    KEEP_ALIVE_FIELD_NUMBER: _ClassVar[int]
    EXCLUDE_INBOUNDS_FIELD_NUMBER: _ClassVar[int]
    type: BackendType
    config: str
    users: _containers.RepeatedCompositeFieldContainer[User]
    keep_alive: int
    exclude_inbounds: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, type: _Optional[_Union[BackendType, str]] = ..., config: _Optional[str] = ..., users: _Optional[_Iterable[_Union[User, _Mapping]]] = ..., keep_alive: _Optional[int] = ..., exclude_inbounds: _Optional[_Iterable[str]] = ...) -> None: ...

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
    __slots__ = ("name", "reset", "type")
    NAME_FIELD_NUMBER: _ClassVar[int]
    RESET_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    name: str
    reset: bool
    type: StatType
    def __init__(self, name: _Optional[str] = ..., reset: bool = ..., type: _Optional[_Union[StatType, str]] = ...) -> None: ...

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

class Latency(_message.Message):
    __slots__ = ("name", "alive", "delay", "link", "last_seen_time", "last_try_time", "source")
    NAME_FIELD_NUMBER: _ClassVar[int]
    ALIVE_FIELD_NUMBER: _ClassVar[int]
    DELAY_FIELD_NUMBER: _ClassVar[int]
    LINK_FIELD_NUMBER: _ClassVar[int]
    LAST_SEEN_TIME_FIELD_NUMBER: _ClassVar[int]
    LAST_TRY_TIME_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    name: str
    alive: bool
    delay: int
    link: str
    last_seen_time: int
    last_try_time: int
    source: str
    def __init__(self, name: _Optional[str] = ..., alive: bool = ..., delay: _Optional[int] = ..., link: _Optional[str] = ..., last_seen_time: _Optional[int] = ..., last_try_time: _Optional[int] = ..., source: _Optional[str] = ...) -> None: ...

class LatencyRequest(_message.Message):
    __slots__ = ("name",)
    NAME_FIELD_NUMBER: _ClassVar[int]
    name: str
    def __init__(self, name: _Optional[str] = ...) -> None: ...

class LatencyResponse(_message.Message):
    __slots__ = ("latencies",)
    LATENCIES_FIELD_NUMBER: _ClassVar[int]
    latencies: _containers.RepeatedCompositeFieldContainer[Latency]
    def __init__(self, latencies: _Optional[_Iterable[_Union[Latency, _Mapping]]] = ...) -> None: ...

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
    __slots__ = ("mem_total", "mem_used", "cpu_cores", "cpu_usage", "incoming_bandwidth_speed", "outgoing_bandwidth_speed", "uptime")
    MEM_TOTAL_FIELD_NUMBER: _ClassVar[int]
    MEM_USED_FIELD_NUMBER: _ClassVar[int]
    CPU_CORES_FIELD_NUMBER: _ClassVar[int]
    CPU_USAGE_FIELD_NUMBER: _ClassVar[int]
    INCOMING_BANDWIDTH_SPEED_FIELD_NUMBER: _ClassVar[int]
    OUTGOING_BANDWIDTH_SPEED_FIELD_NUMBER: _ClassVar[int]
    UPTIME_FIELD_NUMBER: _ClassVar[int]
    mem_total: int
    mem_used: int
    cpu_cores: int
    cpu_usage: float
    incoming_bandwidth_speed: int
    outgoing_bandwidth_speed: int
    uptime: int
    def __init__(self, mem_total: _Optional[int] = ..., mem_used: _Optional[int] = ..., cpu_cores: _Optional[int] = ..., cpu_usage: _Optional[float] = ..., incoming_bandwidth_speed: _Optional[int] = ..., outgoing_bandwidth_speed: _Optional[int] = ..., uptime: _Optional[int] = ...) -> None: ...

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

class Wireguard(_message.Message):
    __slots__ = ("public_key", "peer_ips")
    PUBLIC_KEY_FIELD_NUMBER: _ClassVar[int]
    PEER_IPS_FIELD_NUMBER: _ClassVar[int]
    public_key: str
    peer_ips: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, public_key: _Optional[str] = ..., peer_ips: _Optional[_Iterable[str]] = ...) -> None: ...

class Hysteria(_message.Message):
    __slots__ = ("auth",)
    AUTH_FIELD_NUMBER: _ClassVar[int]
    auth: str
    def __init__(self, auth: _Optional[str] = ...) -> None: ...

class Proxy(_message.Message):
    __slots__ = ("vmess", "vless", "trojan", "shadowsocks", "wireguard", "hysteria")
    VMESS_FIELD_NUMBER: _ClassVar[int]
    VLESS_FIELD_NUMBER: _ClassVar[int]
    TROJAN_FIELD_NUMBER: _ClassVar[int]
    SHADOWSOCKS_FIELD_NUMBER: _ClassVar[int]
    WIREGUARD_FIELD_NUMBER: _ClassVar[int]
    HYSTERIA_FIELD_NUMBER: _ClassVar[int]
    vmess: Vmess
    vless: Vless
    trojan: Trojan
    shadowsocks: Shadowsocks
    wireguard: Wireguard
    hysteria: Hysteria
    def __init__(self, vmess: _Optional[_Union[Vmess, _Mapping]] = ..., vless: _Optional[_Union[Vless, _Mapping]] = ..., trojan: _Optional[_Union[Trojan, _Mapping]] = ..., shadowsocks: _Optional[_Union[Shadowsocks, _Mapping]] = ..., wireguard: _Optional[_Union[Wireguard, _Mapping]] = ..., hysteria: _Optional[_Union[Hysteria, _Mapping]] = ...) -> None: ...

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

class UsersChunk(_message.Message):
    __slots__ = ("users", "index", "last")
    USERS_FIELD_NUMBER: _ClassVar[int]
    INDEX_FIELD_NUMBER: _ClassVar[int]
    LAST_FIELD_NUMBER: _ClassVar[int]
    users: _containers.RepeatedCompositeFieldContainer[User]
    index: int
    last: bool
    def __init__(self, users: _Optional[_Iterable[_Union[User, _Mapping]]] = ..., index: _Optional[int] = ..., last: bool = ...) -> None: ...

class RoutingRule(_message.Message):
    __slots__ = ("outbound_tag", "rule_tag")
    OUTBOUND_TAG_FIELD_NUMBER: _ClassVar[int]
    RULE_TAG_FIELD_NUMBER: _ClassVar[int]
    outbound_tag: str
    rule_tag: str
    def __init__(self, outbound_tag: _Optional[str] = ..., rule_tag: _Optional[str] = ...) -> None: ...

class RoutingRulesResponse(_message.Message):
    __slots__ = ("rules",)
    RULES_FIELD_NUMBER: _ClassVar[int]
    rules: _containers.RepeatedCompositeFieldContainer[RoutingRule]
    def __init__(self, rules: _Optional[_Iterable[_Union[RoutingRule, _Mapping]]] = ...) -> None: ...

class BalancerInfoRequest(_message.Message):
    __slots__ = ("tag",)
    TAG_FIELD_NUMBER: _ClassVar[int]
    tag: str
    def __init__(self, tag: _Optional[str] = ...) -> None: ...

class BalancerInfoResponse(_message.Message):
    __slots__ = ("override_target", "principle_target")
    OVERRIDE_TARGET_FIELD_NUMBER: _ClassVar[int]
    PRINCIPLE_TARGET_FIELD_NUMBER: _ClassVar[int]
    override_target: str
    principle_target: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, override_target: _Optional[str] = ..., principle_target: _Optional[_Iterable[str]] = ...) -> None: ...

class TestRouteRequest(_message.Message):
    __slots__ = ("inbound_tag", "network", "target_ip", "target_domain", "target_port", "protocol", "user", "attributes", "field_selectors", "publish_result")
    class AttributesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    INBOUND_TAG_FIELD_NUMBER: _ClassVar[int]
    NETWORK_FIELD_NUMBER: _ClassVar[int]
    TARGET_IP_FIELD_NUMBER: _ClassVar[int]
    TARGET_DOMAIN_FIELD_NUMBER: _ClassVar[int]
    TARGET_PORT_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    ATTRIBUTES_FIELD_NUMBER: _ClassVar[int]
    FIELD_SELECTORS_FIELD_NUMBER: _ClassVar[int]
    PUBLISH_RESULT_FIELD_NUMBER: _ClassVar[int]
    inbound_tag: str
    network: str
    target_ip: str
    target_domain: str
    target_port: int
    protocol: str
    user: str
    attributes: _containers.ScalarMap[str, str]
    field_selectors: _containers.RepeatedScalarFieldContainer[str]
    publish_result: bool
    def __init__(self, inbound_tag: _Optional[str] = ..., network: _Optional[str] = ..., target_ip: _Optional[str] = ..., target_domain: _Optional[str] = ..., target_port: _Optional[int] = ..., protocol: _Optional[str] = ..., user: _Optional[str] = ..., attributes: _Optional[_Mapping[str, str]] = ..., field_selectors: _Optional[_Iterable[str]] = ..., publish_result: bool = ...) -> None: ...

class RouteResult(_message.Message):
    __slots__ = ("outbound_tag", "outbound_group_tags", "inbound_tag", "network", "target_domain")
    OUTBOUND_TAG_FIELD_NUMBER: _ClassVar[int]
    OUTBOUND_GROUP_TAGS_FIELD_NUMBER: _ClassVar[int]
    INBOUND_TAG_FIELD_NUMBER: _ClassVar[int]
    NETWORK_FIELD_NUMBER: _ClassVar[int]
    TARGET_DOMAIN_FIELD_NUMBER: _ClassVar[int]
    outbound_tag: str
    outbound_group_tags: _containers.RepeatedScalarFieldContainer[str]
    inbound_tag: str
    network: str
    target_domain: str
    def __init__(self, outbound_tag: _Optional[str] = ..., outbound_group_tags: _Optional[_Iterable[str]] = ..., inbound_tag: _Optional[str] = ..., network: _Optional[str] = ..., target_domain: _Optional[str] = ...) -> None: ...

class AddRoutingRuleRequest(_message.Message):
    __slots__ = ("rule", "should_append")
    RULE_FIELD_NUMBER: _ClassVar[int]
    SHOULD_APPEND_FIELD_NUMBER: _ClassVar[int]
    rule: str
    should_append: bool
    def __init__(self, rule: _Optional[str] = ..., should_append: bool = ...) -> None: ...

class RemoveRoutingRuleRequest(_message.Message):
    __slots__ = ("rule_tag",)
    RULE_TAG_FIELD_NUMBER: _ClassVar[int]
    rule_tag: str
    def __init__(self, rule_tag: _Optional[str] = ...) -> None: ...

class OverrideBalancerTargetRequest(_message.Message):
    __slots__ = ("balancer_tag", "target")
    BALANCER_TAG_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    balancer_tag: str
    target: str
    def __init__(self, balancer_tag: _Optional[str] = ..., target: _Optional[str] = ...) -> None: ...
