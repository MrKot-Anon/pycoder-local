"""SOCKS TCP transport. Failure never falls back to a direct connection."""
import ipaddress
import socket
import struct

LEGACY_FIELDS = ('proxy_enabled', 'proxy_version', 'proxy_host', 'proxy_port',
                 'proxy_username', 'proxy_password', 'proxy_remote_dns')
SECOND_FIELDS = ('proxy_chain_enabled', 'proxy_second_version', 'proxy_second_host',
                 'proxy_second_port', 'proxy_second_username', 'proxy_second_password')
FIELDS = LEGACY_FIELDS + ('proxy_chutes', 'proxy_intel', 'proxy_nvidia') + SECOND_FIELDS


def validate(config):
    for key in ('proxy_enabled', 'proxy_remote_dns', 'proxy_chutes', 'proxy_intel', 'proxy_nvidia', 'proxy_chain_enabled'):
        if type(getattr(config, key)) is not bool:
            raise ValueError(f'{key} must be boolean')
    for prefix, active in (('proxy_', config.proxy_enabled),
                           ('proxy_second_', config.proxy_enabled and config.proxy_chain_enabled)):
        version = getattr(config, prefix + 'version')
        port = getattr(config, prefix + 'port')
        if type(version) is not int or version not in (4, 5):
            raise ValueError(f'{prefix}version: SOCKS version must be 4 or 5')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError(f'{prefix}port: port must be 1..65535')
        for name in ('host', 'username', 'password'):
            value = getattr(config, prefix + name)
            if not isinstance(value, str) or '\0' in value or len(value.encode('utf-8')) > 255:
                raise ValueError(f'Invalid {prefix}{name}')
        if active:
            # Numeric endpoints avoid bootstrap DNS outside the chain.
            try:
                ipaddress.ip_address(getattr(config, prefix + 'host'))
            except ValueError:
                raise ValueError('Адрес каждого SOCKS-прокси должен быть IP, чтобы исключить DNS-запрос для самого прокси') from None
            password = getattr(config, prefix + 'password')
            if version == 4 and password:
                raise ValueError('SOCKS4 поддерживает USERID, но не пароль. Для логина/пароля выберите SOCKS5')
            if password and not getattr(config, prefix + 'username'):
                raise ValueError('Для пароля SOCKS5 нужен логин')
    if config.proxy_enabled and config.proxy_chain_enabled and config.proxy_version == 4:
        if ipaddress.ip_address(config.proxy_second_host).version != 4:
            raise ValueError('Первый SOCKS4 не может подключиться ко второму прокси по IPv6; выберите SOCKS5 или IPv4')


def label(config):
    if not config.proxy_enabled:
        return ''
    dns = 'DNS через прокси' if config.proxy_remote_dns else 'DNS локально'
    routes = ' · '.join(f'{name}: ' + ('прокси' if getattr(config, key) else 'напрямую') for name, key in (('Chutes/API', 'proxy_chutes'), ('Intel', 'proxy_intel'), ('NVIDIA', 'proxy_nvidia')))
    def hop(version, host, port):
        return f"SOCKS{version} {('[' + host + ']') if ':' in host else host}:{port}"
    chain = hop(config.proxy_version, config.proxy_host, config.proxy_port)
    if config.proxy_chain_enabled:
        chain += ' → ' + hop(config.proxy_second_version, config.proxy_second_host, config.proxy_second_port)
    from .sandbox import supported
    commands = ('сеть без ограничений PyCoder' if not config.sandbox_enabled or not supported() else
                {'none': 'сеть запрещена', 'allowlist': 'по спискам (прокси / напрямую)',
                 'all': 'прокси', 'direct': 'НАПРЯМУЮ'}[config.sandbox_network])
    return f'ЧЕРЕЗ ПРОКСИ · {chain} · {dns} · {routes} · команды: {commands}'


def read(sock, length):
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise OSError('SOCKS: соединение закрыто')
        data.extend(chunk)
    return bytes(data)


def _handshake(sock, address, version, username, password, remote_dns):
    """Establish a SOCKS CONNECT over an existing stream, including a tunnel."""
    host, port = address
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is None and not remote_dns:
        family = socket.AF_INET if version == 4 else socket.AF_UNSPEC
        ip = ipaddress.ip_address(socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)[0][4][0])
    domain = host.encode('idna') if ip is None else b''
    if len(domain) > 255 or b'\0' in domain:
        raise OSError('SOCKS: некорректное имя хоста')
    user, password = username.encode(), password.encode()
    if version == 4:
        if ip is not None and ip.version != 4:
            raise OSError('SOCKS4 не поддерживает IPv6')
        sock.sendall(b'\x04\x01' + struct.pack('!H', port) + (ip.packed if ip else b'\0\0\0\1')
                     + user + b'\0' + (domain + b'\0' if ip is None else b''))
        reply = read(sock, 8)
        if reply[:2] != b'\0\x5a':
            raise OSError('SOCKS4: подключение отклонено')
    else:
        method = 2 if user else 0
        sock.sendall(bytes((5, 1, method)))
        if read(sock, 2) != bytes((5, method)):
            raise OSError('SOCKS5: метод авторизации отклонён')
        if method == 2:
            sock.sendall(bytes((1, len(user))) + user + bytes((len(password),)) + password)
            if read(sock, 2) != b'\x01\0':
                raise OSError('SOCKS5: ошибка авторизации')
        target = (bytes((1 if ip.version == 4 else 4,)) + ip.packed) if ip else bytes((3, len(domain))) + domain
        sock.sendall(b'\x05\x01\0' + target + struct.pack('!H', port))
        reply = read(sock, 4)
        if reply[:3] != b'\x05\0\0' or reply[3] not in (1, 3, 4):
            raise OSError('SOCKS5: подключение отклонено')
        size = {1: 4, 4: 16}.get(reply[3])
        read(sock, (read(sock, 1)[0] if size is None else size) + 2)


def connect(config, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    validate(config)
    if not config.proxy_enabled:
        return socket.create_connection(address, timeout, source_address)
    # This is the only physical dial: neither hop 2 nor the target is dialled locally.
    sock = socket.create_connection((config.proxy_host, config.proxy_port), timeout, source_address)
    stage = 1
    try:
        first_target = (config.proxy_second_host, config.proxy_second_port) if config.proxy_chain_enabled else address
        _handshake(sock, first_target, config.proxy_version, config.proxy_username,
                   config.proxy_password, config.proxy_remote_dns)
        if config.proxy_chain_enabled:
            stage = 2
            _handshake(sock, address, config.proxy_second_version, config.proxy_second_username,
                       config.proxy_second_password, config.proxy_remote_dns)
        return sock
    except OSError as error:
        sock.close()
        raise OSError(f'SOCKS: звено {stage}: {error}') from None
    except BaseException:
        sock.close()
        raise


def configure(config, ask, secret, choose):
    """Explicit startup configuration, before any requests or child processes."""
    config.proxy_enabled = choose('Использовать SOCKS-прокси?', [('yes', 'Да'), ('no', 'Нет')]) == 'yes'
    if config.proxy_enabled:
        config.proxy_version = int(choose('Версия SOCKS', [('5', 'SOCKS5'), ('4', 'SOCKS4 / SOCKS4a')]))
        config.proxy_host = ask('IP прокси: ').strip()
        config.proxy_port = int(ask('Порт прокси [1080]: ').strip() or '1080')
        config.proxy_username = ask('Логин / SOCKS4 USERID (пусто — без авторизации): ')
        config.proxy_password = secret('Пароль SOCKS5: ') if config.proxy_version == 5 and config.proxy_username else ''
        config.proxy_chain_enabled = choose('Использовать второй SOCKS через первый?', [('no', 'Нет, один прокси'), ('yes', 'Да, цепочка: первый → второй → сервис')]) == 'yes'
        if config.proxy_chain_enabled:
            config.proxy_second_version = int(choose('Версия второго SOCKS', [('5', 'SOCKS5'), ('4', 'SOCKS4 / SOCKS4a')]))
            config.proxy_second_host = ask('IP второго прокси: ').strip()
            config.proxy_second_port = int(ask('Порт второго прокси [1080]: ').strip() or '1080')
            config.proxy_second_username = ask('Логин / USERID второго прокси (пусто — без авторизации): ')
            config.proxy_second_password = secret('Пароль второго SOCKS5: ') if config.proxy_second_version == 5 and config.proxy_second_username else ''
        config.proxy_remote_dns = choose('DNS через последний прокси?', [('yes', 'Да (SOCKS4a / SOCKS5)'), ('no', 'Нет, разрешать имена локально')]) == 'yes'
        for service, title in (('chutes', 'Chutes / API модели'), ('intel', 'Intel'), ('nvidia', 'NVIDIA')):
            setattr(config, 'proxy_' + service, choose(title + ': маршрут', [('yes', 'Через прокси'), ('no', 'Напрямую')]) == 'yes')
    validate(config)


def for_service(config, service):
    from dataclasses import replace
    if service not in {'chutes', 'intel', 'nvidia'}:
        raise ValueError('Unknown proxy service')
    return replace(config, proxy_enabled=config.proxy_enabled and getattr(config, 'proxy_' + service))


def attestation_route(config, host):
    host = host.lower().rstrip('.')
    for service, domain in (('nvidia', 'nvidia.com'), ('intel', 'intel.com')):
        if host == domain or host.endswith('.' + domain):
            return for_service(config, service)
    # Unknown/redirect destinations retain the strict master proxy route.
    return config


def save(config, directory):
    from .util import private_json
    validate(config)
    private_json(directory / "proxy.json", {key: getattr(config, key) for key in FIELDS})
