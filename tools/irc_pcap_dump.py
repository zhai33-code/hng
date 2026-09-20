#!/usr/bin/env python3
"""Extract IRC (port 16667) TCP streams from pcapng captures into a readable transcript.

Walks packets in capture order, buffers per-direction, and emits completed IRC
lines with direction tags (C>S / S>C). NUL/CTCP/color bytes are escaped.

Usage: python irc_pcap_dump.py [pcap_glob ...] [-o OUTPUT]
Defaults: D:\\HnG-test\\*.pcapng -> work/hng-server-re/irc-official-transcript.txt
"""
import glob
import sys
import argparse
from pathlib import Path

try:
    from scapy.all import rdpcap, TCP, IP, IPv6
except ImportError:
    sys.exit("scapy required: pip install scapy")

PORT = 16667


def stream_key(pkt):
    """Canonical key for a TCP flow: (lower_ip, lower_port, higher_ip, higher_port)."""
    ip_src = pkt[IP].src if IP in pkt else pkt[IPv6].src
    ip_dst = pkt[IP].dst if IP in pkt else pkt[IPv6].dst
    sp, dp = pkt[TCP].sport, pkt[TCP].dport
    if (ip_src, sp) <= (ip_dst, dp):
        return (ip_src, sp, ip_dst, dp)
    return (ip_dst, dp, ip_src, sp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="*", default=[r"D:\HnG-test\*.pcapng"])
    ap.add_argument("-o", "--output", default=None)
    args = ap.parse_args()

    files = []
    for pat in args.inputs:
        files.extend(glob.glob(pat))
    if not files:
        sys.exit("no pcap files matched")

    out_path = Path(args.output) if args.output else Path(__file__).resolve().parents[2] / "irc-official-transcript.txt"
    report = []
    n_streams = 0
    for f in sorted(files):
        pkts = rdpcap(f)
        # collect flow keys (server port 16667) in this capture
        keys = []
        for pkt in pkts:
            if TCP not in pkt or not bytes(pkt[TCP].payload):
                continue
            if pkt[TCP].dport != PORT and pkt[TCP].sport != PORT:
                continue
            key = stream_key(pkt)
            if key not in keys:
                keys.append(key)

        if not keys:
            continue
        report.append("=" * 100)
        report.append(f"PCAP: {f}")
        for key in keys:
            n_streams += 1
            report.append("-" * 100)
            report.append(f"STREAM {key[0]}:{key[1]} <-> {key[2]}:{key[3]}")
            buf = {"a": b"", "b": b""}
            # key = (ipA, portA, ipB, portB); server side is the one with port==16667
            if key[1] == PORT:
                label = {"b": "C>S", "a": "S>C"}
            else:
                label = {"a": "C>S", "b": "S>C"}
            for pkt in pkts:
                if TCP not in pkt or not bytes(pkt[TCP].payload):
                    continue
                if pkt[TCP].dport != PORT and pkt[TCP].sport != PORT:
                    continue
                if stream_key(pkt) != key:
                    continue
                ip_src = pkt[IP].src if IP in pkt else pkt[IPv6].src
                sp = pkt[TCP].sport
                d = "a" if (ip_src, sp) == (key[0], key[1]) else "b"
                buf[d] += bytes(pkt[TCP].payload)
                while True:
                    idx = buf[d].find(b"\n")
                    if idx < 0:
                        break
                    line = buf[d][:idx].rstrip(b"\r")
                    buf[d] = buf[d][idx + 1:]
                    txt = line.decode("utf-8", errors="replace")
                    txt = txt.replace("\x00", "\\0").replace("\x01", "\\001").replace("\x03", "\\003")
                    report.append(f"[{label[d]}] {txt}")
            for d in ("a", "b"):
                if buf[d].strip():
                    txt = buf[d].decode("utf-8", errors="replace")
                    report.append(f"[{label[d]}] <no-newline tail> {txt!r}")
        report.append("")

    out_path.write_text("\n".join(report), encoding="utf-8")
    print(f"{len(files)} pcaps, {n_streams} IRC streams -> {out_path}")


if __name__ == "__main__":
    main()
