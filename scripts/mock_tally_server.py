from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import re


MOCK_DATA = {
    "COMPANY": [
        {"NAME": "NOKVO Demo Pvt Ltd", "GUID": "company-guid-001", "STARTINGFROM": "20240401", "BOOKSFROM": "20240401"},
    ],
    "GROUP": [
        {"NAME": "Sundry Debtors", "PARENT": "Current Assets", "PRIMARYGROUP": "Yes", "GUID": "group-guid-001"},
        {"NAME": "Bank Accounts", "PARENT": "Current Assets", "PRIMARYGROUP": "Yes", "GUID": "group-guid-002"},
    ],
    "LEDGER": [
        {
            "NAME": "Acme Retail",
            "PARENT": "Sundry Debtors",
            "OPENINGBALANCE": "0",
            "CLOSINGBALANCE": "125000.00",
            "GSTREGISTRATIONTYPE": "Regular",
            "PARTYGSTIN": "29ABCDE1234F1Z5",
            "GUID": "ledger-guid-001",
        },
        {
            "NAME": "HDFC Bank",
            "PARENT": "Bank Accounts",
            "OPENINGBALANCE": "500000.00",
            "CLOSINGBALANCE": "735000.00",
            "GUID": "ledger-guid-002",
        },
    ],
    "STOCKGROUP": [
        {"NAME": "Finished Goods", "PARENT": "Primary", "GUID": "stock-group-guid-001"},
    ],
    "STOCKITEM": [
        {
            "NAME": "NOKVO Voice Seat",
            "PARENT": "Finished Goods",
            "BASEUNITS": "Nos",
            "OPENINGBALANCE": "10 Nos",
            "CLOSINGBALANCE": "7 Nos",
            "GUID": "stock-item-guid-001",
        },
    ],
    "UNIT": [
        {"NAME": "Nos", "ORIGINALNAME": "Numbers", "ISSIMPLEUNIT": "Yes", "GUID": "unit-guid-001"},
    ],
    "COSTCENTRE": [
        {"NAME": "Sales Team", "PARENT": "Primary Cost Category", "CATEGORY": "Primary Cost Category", "GUID": "cc-guid-001"},
    ],
    "VOUCHERTYPE": [
        {"NAME": "Sales", "PARENT": "Sales", "NUMBERINGMETHOD": "Automatic", "GUID": "vt-guid-001"},
        {"NAME": "Receipt", "PARENT": "Receipt", "NUMBERINGMETHOD": "Automatic", "GUID": "vt-guid-002"},
    ],
    "VOUCHER": [
        {
            "DATE": "20260430",
            "VOUCHERTYPENAME": "Sales",
            "VOUCHERNUMBER": "S-001",
            "PARTYLEDGERNAME": "Acme Retail",
            "AMOUNT": "125000.00",
            "GUID": "voucher-guid-001",
        },
    ],
}


def extract_requested_type(xml_payload: str) -> str:
    matches = re.findall(r"<TYPE>\s*([^<]+)\s*</TYPE>", xml_payload, flags=re.IGNORECASE)
    if not matches:
        return "COMPANY"
    requested = matches[-1].strip().upper().replace(" ", "")
    aliases = {
        "STOCKGROUP": "STOCKGROUP",
        "STOCKITEM": "STOCKITEM",
        "COSTCENTRE": "COSTCENTRE",
        "VOUCHERTYPE": "VOUCHERTYPE",
    }
    return aliases.get(requested, requested)


def xml_response_for(requested_type: str) -> str:
    records = MOCK_DATA.get(requested_type, [])
    body = []
    for record in records:
        fields = "".join(f"<{key}>{value}</{key}>" for key, value in record.items())
        name_attr = f' NAME="{record["NAME"]}"' if "NAME" in record else ""
        body.append(f"<{requested_type}{name_attr}>{fields}</{requested_type}>")
    return f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <STATUS>1</STATUS>
  </HEADER>
  <BODY>
    <DATA>
      <COLLECTION>
        {''.join(body)}
      </COLLECTION>
    </DATA>
  </BODY>
</ENVELOPE>"""


class MockTallyHandler(BaseHTTPRequestHandler):
    server_version = "MockTally/1.0"

    def do_GET(self) -> None:
        self._send_response(xml_response_for("COMPANY"))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        payload = self.rfile.read(length).decode("utf-8", errors="replace")
        requested_type = extract_requested_type(payload)
        print(f"[mock-tally] {self.client_address[0]} requested {requested_type}", flush=True)
        self._send_response(xml_response_for(requested_type))

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send_response(self, response_xml: str) -> None:
        body = response_xml.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a fake Tally XML HTTP server for local integration testing.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), MockTallyHandler)
    print(f"[mock-tally] listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
