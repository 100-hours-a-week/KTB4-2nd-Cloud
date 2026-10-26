"""Exact V1 staging capacity transform and an isolated, dual-stack nftables table."""
import re

TABLE = 'yeodam_tcp436'


def capacity(text, connections, nofile=4096):
    if connections not in (768, 1536, 2048) or nofile != 4096:
        raise ValueError('Unmeasured capacity candidate')
    expression = r'(?m)^(\s*)worker_connections\s+\d+;'
    if len(re.findall(expression, text)) != 1 or len(re.findall(r'(?m)^\s*worker_processes\s+auto;', text)) != 1:
        raise ValueError('Inspect existing worker configuration')
    text = re.sub(expression, lambda m: m[1]+'worker_connections '+str(connections)+';', text)
    expression = r'(?m)^[ \t]*worker_rlimit_nofile[ \t]+\d+;'
    count = len(re.findall(expression, text))
    if count > 1:
        raise ValueError('Ambiguous worker FD limit')
    if count:
        text = re.sub(expression, 'worker_rlimit_nofile 4096;', text)
    else:
        text = re.sub(r'(?m)^(worker_processes\s+auto;)', r'\1\nworker_rlimit_nofile 4096;', text)
    return text


def nft_policy(connections, rate, burst, enforce):
    if connections not in (0, 16, 32, 64, 128, 256) or rate not in (0, 10, 20, 30, 60):
        raise ValueError('Outside bounded comparison candidates')
    if not 1 <= burst <= 120 or (not connections and not rate):
        raise ValueError('At least one guard and explicit bounded burst required')
    declarations, rules = [], ['        iifname "lo" return']
    for family, addr in [('ipv4', 'ip'), ('ipv6', 'ip6')]:
        number = '4' if family == 'ipv4' else '6'
        condition = 'meta nfproto '+family+' tcp dport { 80, 443 } ct state new tcp flags & (fin | syn | rst | ack) == syn '
        action = ' drop' if enforce else ''
        if connections:
            declarations += ['    set concurrent'+number+' { type '+family+'_addr; flags dynamic; size 16384; }',
                             '    counter concurrent'+number+' { }']
            rules.append('        '+condition+'add @concurrent'+number+' { '+addr+' saddr ct count over '+str(connections)+' } counter name concurrent'+number+action)
        if rate:
            declarations += ['    set rate'+number+' { type '+family+'_addr; flags dynamic,timeout; timeout 60s; size 16384; }',
                             '    counter rate'+number+' { }']
            rules.append('        '+condition+'update @rate'+number+' { '+addr+' saddr limit rate over '+str(rate)+'/second burst '+str(burst)+' packets } counter name rate'+number+action)
    # Only this table is managed. Never flush the host/Docker/UFW ruleset.
    return 'table inet '+TABLE+' {\n'+'\n'.join(declarations)+'\n    chain ingress {\n        type filter hook input priority -10; policy accept;\n'+'\n'.join(rules)+'\n    }\n}\n'
