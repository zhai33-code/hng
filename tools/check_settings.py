SETTINGS_JS = b"""\
flash_dynhttp="127.0.0.1:15110";
unique_file_servers="127.0.0.1";
ircserver="127.0.0.1:16667";
version="173293";
releasename="QQ";
distribution="retoxdistribution";
language="en";
account_url="http://127.0.0.1/account";
uniqueversionfilename="hng_unique_v173293";
localizerFile="texts/HnG_Strategy_en.xml";
siteonly="false";
"""

print('len:', len(SETTINGS_JS))
print()

for i, line in enumerate(SETTINGS_JS.split(b'\n')):
    print(f'line {i}: {line!r}')

# Check for \R (uppercase R) preceded by whitespace
import re
matches = [(m.start(), m.group()) for m in re.finditer(rb'\s*R', SETTINGS_JS)]
print()
print('whitespace+R matches:', matches)

# Check each line's split("=") result
print()
for i, line in enumerate(SETTINGS_JS.decode().split('\n')):
    if '=' in line:
        parts = line.split('=')
        key = parts[0]
        val = parts[1].replace('"', '', 1).replace('";', '', 1) if len(parts) > 1 else ''
        print(f'  key={key!r}  val={val!r}  parts={len(parts)}')
