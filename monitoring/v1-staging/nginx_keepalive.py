"""Change only the V1 staging HTTPS client idle timeout; preserve the read policy."""
import re
from nginx_read_enforcement import instrument_read_enforcement

BEGIN = '    # BEGIN yeodam staging idle keepalive'
END = '    # END yeodam staging idle keepalive'
BLOCK = re.compile(r'\n'+re.escape(BEGIN)+r'\n    keepalive_timeout (15|30)s;\n'+re.escape(END))


def instrument_keepalive(text, seconds):
    if seconds not in (15,30,75):
        raise ValueError('Only measured comparison candidates15/30 or baseline75 are allowed')
    matches=list(BLOCK.finditer(text))
    if text.count(BEGIN)!=len(matches) or text.count(END)!=len(matches) or len(matches)>1:
        raise ValueError('Partial or unknown idle policy')
    base=BLOCK.sub('',text)
    if re.search(r'^\s*keepalive_timeout\s',base,re.M):
        raise ValueError('Existing timeout requires explicit review')
    if instrument_read_enforcement(base)!=base:
        raise ValueError('Expected existing enforced read policy')
    if seconds==75:
        return base
    marker='    server_name v1-staging.yeodam-2gether.com;'
    if base.count(marker)!=2:
        raise ValueError('Expected exact V1 staging HTTP/HTTPS servers')
    position=base.rindex(marker)+len(marker)
    if not re.search(r'listen\s+443\s+ssl',base[base.rindex('server {',0,position):position]):
        raise ValueError('Expected HTTPS server')
    return base[:position]+'\n'+BEGIN+'\n    keepalive_timeout '+str(seconds)+'s;\n'+END+base[position:]
