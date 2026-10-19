# Supervised Nobitex Live Read-Only USDT Smoke Test

## What this tool does

`scripts/live_readonly_nobitex_usdt.py` performs one read-only check against the official
Nobitex API. It reads the USDT Spot wallet and USDT Margin wallet separately using only
`GET /v2/wallets` with `currencies=usdt` and `type=spot|margin`.

A dedicated `WalletOnlyNobitexTransport` rejects every request except this exact wallet-read
contract before delegating to the existing HTTPS transport. It rejects all POST requests,
including the read-only order-status query, because this smoke test does not need them.

The tool prints a minimal JSON snapshot to stdout: wallet type, currency, observation timestamp,
total balance, blocked balance and available balance. It does not print raw API responses, profile
data, bank details, account IDs, authentication headers or token values. Errors are reported by
exception class only, without dumping exception text or a traceback.

## Preconditions

1. Create or select an API token through Nobitex's official account controls.
2. Verify in Nobitex that the token has **no trading and no withdrawal permissions**. The code
   cannot prove the remote server-side authorization scope from the token string or variable name.
   Do not use an ordinary broad-permission token for this test.
3. Never put the token in source code, `.env.example`, command-line arguments, commands saved
   to shell history, GitHub comments or chat.
4. Use a trusted local terminal. The token will be available as a process environment variable
   during execution; clear it immediately afterwards.
5. Install EVORYN and development dependencies once with no token configured:
   `python -m pip install -e ".[dev]"`

## Windows PowerShell

Run from the repository root. This prompts without echoing the token; the script reads the
environment variable and never accepts the token as a CLI argument.

```powershell
$secure = Read-Host "Enter a Nobitex token verified as read-only" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$exitCode = 1
try {
    $env:NOBITEX_READONLY_API_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    python scripts/live_readonly_nobitex_usdt.py
    $exitCode = $LASTEXITCODE
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    Remove-Item Env:NOBITEX_READONLY_API_TOKEN -ErrorAction SilentlyContinue
    Remove-Variable secure, bstr -ErrorAction SilentlyContinue
}
if ($exitCode -ne 0) { throw "Nobitex read-only smoke test failed with exit code $exitCode" }
```

## macOS / Linux shell

Run from the repository root. `read -s` prevents terminal echo; unset the variable after the test.

```bash
read -r -s -p "Enter a Nobitex token verified as read-only: " NOBITEX_READONLY_API_TOKEN
printf '\n'
export NOBITEX_READONLY_API_TOKEN
python scripts/live_readonly_nobitex_usdt.py
status=$?
unset NOBITEX_READONLY_API_TOKEN
exit "$status"
```

## Output semantics

- `status=ok` means both wallet reads returned successful, parseable responses.
- `presence=present` means the response included the USDT wallet and valid amounts.
- `presence=not_listed` means the response omitted that wallet. Its balance stays `null`;
  it is not treated as zero.
- `available_balance` is calculated exactly using Decimal arithmetic as `balance - blocked`.
- A nonzero exit means the read-only check failed or configuration was invalid. The script
  suppresses response bodies and exception details.

## Explicit non-goals

The tool implements no order creation, order cancellation, order-status query, margin-order
endpoint, withdrawal, transfer, collateral change, user-profile read, retry loop, market scan,
persistent logging or background scheduler. It makes exactly two bounded wallet reads when both
API calls succeed.

CI uses fake transports only. Green CI verifies local no-write guards and response normalization;
it does not prove a live token exists, has the required provider-side scope, or can reach Nobitex
from the target runtime. The live check remains a supervised operational action.
