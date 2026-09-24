"""Weekly full-address recurring unnamed-wallet report for private Discord."""
from audit_mtd_wallet_aliases import audit


def render():
    result = audit(include_full=True)
    scope = result['scope']
    heading = '**MTD Wallet Alias Audit — saved data only**'
    detail = (f"Scope: {scope['active_exotic_listings']} active Exotic listings; "
              f"{scope['last_exotic_sales']} last-Exotic sales; "
              f"{scope['recent_completed_sales']} recent completed sales; "
              f"{scope['tracked_wallet_histories']} tracked-wallet histories. No live calls.")
    if not result['wallets']:
        return heading + '\nNo recurring unnamed wallets found.\n' + detail
    lines = [heading]
    for row in result['wallets']:
        sources = ', '.join(f'{name}: {count}' for name, count in row['sources'].items())
        roles = ', '.join(f'{name}: {count}' for name, count in row['roles'].items())
        lines.append(f"• `{row['full_wallet']}` — {row['distinct_occurrences']} distinct occurrences — {sources} — {roles}")
    lines.append(detail)
    return '\n'.join(lines)


if __name__ == '__main__':
    print(render())
