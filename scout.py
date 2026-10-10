#!/usr/bin/env python3

import sys
import csv
import time
import os
import logging
from pathlib import Path

if sys.platform == 'win32':
    os.system('chcp 65001 >nul 2>&1')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).parent))

from app import __version__

_verbose = '--verbose' in sys.argv or '-V' in sys.argv
logging.basicConfig(
    level=logging.DEBUG if _verbose else logging.WARNING,
    format='%(name)s: %(message)s',
)

_args = [a for a in sys.argv[1:] if a not in ('--verbose', '-V')]
if _args:
    arg = _args[0].lower()
    if arg in ('--version', '-v', 'version'):
        print(f"Scout v{__version__}")
        sys.exit(0)
    elif arg in ('--help', '-h', 'help'):
        print(f"Scout v{__version__} - lead scouting and SPOC contact enrichment")
        print()
        print("Usage: python scout.py [options]")
        print()
        print("Options:")
        print("  --version, -v    Show version")
        print("  --help, -h       Show this help")
        print("  --verbose, -V    Enable debug logging")
        print()
        print("Run without arguments to start the interactive menu.")
        sys.exit(0)

env_file = Path(__file__).parent / '.env'
if env_file.exists():
    with open(env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _key, _, _val = _line.partition('=')
                os.environ.setdefault(_key.strip(), _val.strip())

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich import box
from rich.theme import Theme
from rich.rule import Rule

from app.scouting.instagram import scrape_profile_no_login
from app.common.net import random_delay, get_httpx_proxy

ACCENT = "#a70947"
ACCENT_DIM = "#6b0530"
ACCENT_LIGHT = "#d64d7a"
ACCENT_MUTED = "#4a0727"



_session_stats = {"scraped": 0}
custom_theme = Theme({
    "prompt.choices": ACCENT,
    "prompt.default": "dim",
})
console = Console(theme=custom_theme)
Prompt.prompt_suffix = " "
Confirm.prompt_suffix = " "


def _update_env(key, value):
    env_path = Path(__file__).parent / '.env'
    lines = []
    found = False
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith(key + '='):
                    lines.append(f'{key}={value}\n')
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f'{key}={value}\n')
    with open(env_path, 'w') as f:
        f.writelines(lines)
    os.environ[key] = value


LINK_HOSTS = ('linktr.ee', 'instagram.com', 'tiktok.com', 'youtube.com', 'youtu.be', 'twitter.com', 'x.com',
              'facebook.com', 'stan.store', 'beacons.ai', 'bit.ly', 'spotify.com')


def _spoc_from_profile(p):
    site = p.get('website') or ''
    if any(h in site.lower() for h in LINK_HOSTS):
        site = ''                        # a link-in-bio page is not the person's company site
    return {'lead_id': p.get('username'), 'name': p.get('full_name', ''), 'role': p.get('headline', ''),
            'website': site, 'email': p.get('email', ''), 'phone': p.get('phone', '')}


def enrich_profiles(profiles):
    """Complete and verify each scraped lead's email/phone with the same engine enrich_cli.py uses."""
    if not profiles:
        return profiles

    if not Confirm.ask("\n[white][+] Enrich leads with contact info?[/white]", default=True):
        return profiles

    from concurrent.futures import ThreadPoolExecutor
    from app.enrichment.spoc import SpocFinder
    finder = SpocFinder()

    console.print()
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
        task = progress.add_task("[white]Enriching leads...", total=len(profiles))

        def _one(p):
            try:
                r = finder.find(_spoc_from_profile(p))
            except Exception as ex:
                logging.getLogger(__name__).debug(f"enrich failed: {ex}")
                return p
            finally:
                progress.advance(task)
            email = r['email'] or p.get('email', '')
            status = r['email_status'] if r['email'] else ('UNVERIFIED' if email else 'NOT_FOUND')
            return {**p, 'email': email, 'email_status': status, 'email_confidence': r['email_confidence'],
                    'email_sources': ','.join(r['email_sources']), 'phone': r['phone'] or p.get('phone', ''),
                    'phone_scope': r['phone_scope'], 'enrichment_gaps': ','.join(r['gaps'])}

        with ThreadPoolExecutor(max_workers=4) as ex:
            enriched = list(ex.map(_one, profiles))

    console.print()
    verified = sum(e.get('email_status') == 'VERIFIED' for e in enriched)
    emails = sum(bool(e.get('email')) for e in enriched)
    phones = sum(bool(e.get('phone')) for e in enriched)
    console.print(Rule("[bold white]Enrichment[/bold white]", style=ACCENT_DIM, align="left"))
    console.print(f"\n  [green]{emails} emails ({verified} verified)[/green]  [green]{phones} phones[/green]  "
                  f"[dim]of {len(enriched)} leads[/dim]\n")

    email_leads = [e for e in enriched if e.get('email')]
    if email_leads:
        t = Table(show_header=True, box=box.MINIMAL_HEAVY_HEAD, border_style="dim", padding=(0, 1))
        for col in ("Lead", "Email", "Status", "Confidence", "Sources"):
            t.add_column(col, style="dim" if col == "Sources" else "white")
        for e in email_leads:
            conf = e.get('email_confidence', 0)
            color = 'green' if conf >= 80 else 'yellow' if conf >= 50 else 'red'
            t.add_row((e.get('full_name') or e.get('username', '?'))[:25], e['email'], e.get('email_status', '?'),
                      f"[{color}]{conf}%[/{color}]", e.get('email_sources', ''))
        console.print(t)

    return enriched


def _platform_header(name: str, subtitle: str = "No login required"):
    """Print a consistent platform header."""
    console.print()
    console.print(Rule(
        f"[bold white]{name}[/bold white]  [dim]{subtitle}[/dim]",
        style=ACCENT_DIM,
        align="left"
    ))
    console.print()


def _profile_card(profile: dict):
    """Display a scraped profile as a compact card."""
    _session_stats["scraped"] += 1
    lines = []

    if profile.get('full_name'):
        lines.append(f"[bold white]{profile['full_name'][:50]}[/bold white]")

    stats = []
    if profile.get('follower_count'):
        stats.append(f"[white]{profile['follower_count']:,}[/white] [dim]followers[/dim]")
    if profile.get('following_count'):
        stats.append(f"[white]{profile['following_count']:,}[/white] [dim]following[/dim]")
    if stats:
        lines.append("  ·  ".join(stats))

    email = profile.get('email', '')
    if profile.get('bio'):
        bio = profile['bio'].replace('\n', ' ')
        if email:
            bio = bio.replace(email, '').strip(' |·-,')
        bio = bio[:80] + '...' if len(bio) > 80 else bio
        if bio:
            lines.append(f"[dim]{bio}[/dim]")

    if email:
        lines.append(f"[bold {ACCENT}]{email}[/bold {ACCENT}]")

    if profile.get('website'):
        lines.append(f"[dim]{profile['website']}[/dim]")

    if profile.get('phone'):
        lines.append(f"[dim]{profile['phone']}[/dim]")

    username = profile.get('username', '?')
    console.print(Panel(
        "\n".join(lines) if lines else "[dim]No data[/dim]",
        title=f"[bold white]@{username}[/bold white]",
        title_align="left",
        border_style=ACCENT_DIM,
        box=box.ROUNDED,
        padding=(0, 2),
        width=64
    ))


def _success_summary(successful: int, total: int, item_type: str = "profiles"):
    """Print a compact success summary line."""
    console.print()
    if successful == total:
        console.print(f"  [green]OK[/green] [white]{successful}/{total} {item_type} scraped[/white]")
    elif successful > 0:
        console.print(f"  [yellow]DONE[/yellow] [white]{successful}/{total} {item_type} scraped[/white]")
    else:
        console.print(f"  [red]FAIL[/red] [white]0/{total} {item_type} scraped[/white]")
    console.print()


def _export_result(filename: str, count: int, item_type: str = "profiles"):
    """Print a compact export confirmation line."""
    console.print(f"  [green]Saved[/green] [white]{filename}[/white] [dim]({count} {item_type})[/dim]")
    console.print()


def _no_results():
    """Print no-results feedback."""
    console.print()
    console.print("  [yellow]No profiles were scraped successfully[/yellow]")
    console.print()


def _pause():
    """Pause before returning to menu so the user can review output."""
    console.print()
    console.print(Rule(style=ACCENT_MUTED))
    Prompt.ask("[dim]Press Enter to continue[/dim]", default="")


def show_header():
    proxy_str = "[green]● on[/green]" if get_httpx_proxy() else "[dim]○ off[/dim]"
    status = f"[dim]github.com/Tanv1n1/ScoutLeads[/dim]  ·  [dim]Proxy:[/dim] {proxy_str}"
    if _session_stats["scraped"] > 0:
        status += f"  ·  [dim]Scraped:[/dim] [white]{_session_stats['scraped']}[/white]"
    console.print()
    console.print(f"[bold {ACCENT}]SCOUT[/bold {ACCENT}]  [dim]v{__version__}  lead scouting & enrichment[/dim]")
    console.print(status)
    console.print()


def show_menu():
    console.print(Rule("[bold white]Platforms[/bold white]", style=ACCENT_DIM, align="left"))
    console.print()

    platforms_left = [
        ("1", "Instagram", "profiles"),
        ("2", "TikTok", "profiles"),
        ("3", "LinkedIn", "cookie"),
        ("4", "GitHub", "profiles"),
        ("5", "YouTube", "channels"),
    ]
    platforms_right = [
        ("6", "Twitch", "streamers"),
        ("7", "Linktree", "link-in-bio"),
        ("8", "Pinterest", "profiles"),
    ]

    table = Table(show_header=False, box=None, padding=(0, 0), pad_edge=False, expand=False)
    table.add_column("", width=35)
    table.add_column("", width=35)

    max_rows = max(len(platforms_left), len(platforms_right))
    for i in range(max_rows):
        left = ""
        right = ""
        if i < len(platforms_left):
            n, name, desc = platforms_left[i]
            left = f"  [{ACCENT}][{n}][/{ACCENT}]  [bold white]{name}[/bold white]  [dim]{desc}[/dim]"
        if i < len(platforms_right):
            n, name, desc = platforms_right[i]
            right = f"  [{ACCENT}][{n}][/{ACCENT}]  [bold white]{name}[/bold white]  [dim]{desc}[/dim]"
        table.add_row(left, right)

    console.print(table)
    console.print()

    console.print(Rule("[bold white]Tools[/bold white]", style=ACCENT_DIM, align="left"))
    console.print()

    tools_left = [
        ("9", "Bulk Scrape", "from file"),
        ("10", "Exports", "view files"),
        ("12", "Discover", "cross-border biz"),
    ]
    tools_right = [
        ("11", "Settings", "config"),
        ("0", "Exit", ""),
    ]

    tools = Table(show_header=False, box=None, padding=(0, 0), pad_edge=False, expand=False)
    tools.add_column("", width=35)
    tools.add_column("", width=35)

    for i in range(max(len(tools_left), len(tools_right))):
        left = ""
        right = ""
        if i < len(tools_left):
            n, name, desc = tools_left[i]
            left = f"  [{ACCENT}][{n}][/{ACCENT}]  [bold white]{name}[/bold white]  [dim]{desc}[/dim]"
        if i < len(tools_right):
            n, name, desc = tools_right[i]
            right = f"  [{ACCENT}][{n}][/{ACCENT}]  [bold white]{name}[/bold white]  [dim]{desc}[/dim]"
        tools.add_row(left, right)

    console.print(tools)
    console.print()


def _get_delay_range(fallback=(1.0, 2.5)):
    """Get configured delay range from env vars, with fallback."""
    try:
        d_min = float(os.environ.get('SCOUT_DELAY_MIN', str(fallback[0])))
        d_max = float(os.environ.get('SCOUT_DELAY_MAX', str(fallback[1])))
        return (d_min, d_max) if d_max >= d_min >= 0 else fallback
    except ValueError:
        return fallback


def _standard_scrape_loop(scraper_func, items, label_prefix="@", delay_range=None):
    """Shared scrape loop: spinner per item, then profile card. Clean output."""
    if delay_range is None:
        delay_range = _get_delay_range()
    profiles = []

    for i, item in enumerate(items, 1):
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console, transient=True) as progress:
            task = progress.add_task(f"[white]Scraping {label_prefix}{item}  [dim]({i}/{len(items)})[/dim]", total=None)
            try:
                prev_level = logging.getLogger().level
                if not _verbose:
                    logging.getLogger().setLevel(logging.CRITICAL)
                profile = scraper_func(item)
                if not _verbose:
                    logging.getLogger().setLevel(prev_level)

                if profile:
                    profiles.append(profile)
                    progress.stop()
                    _profile_card(profile)
                else:
                    progress.stop()
                    console.print(f"  [red]✗[/red] [dim]{label_prefix}{item} — not found[/dim]")
            except RuntimeError as e:
                if not _verbose:
                    logging.getLogger().setLevel(prev_level)
                progress.stop()
                console.print(f"  [bold red]✗ {label_prefix}{item} — rate limited[/bold red]")
                break
            except Exception as e:
                if not _verbose:
                    logging.getLogger().setLevel(prev_level)
                progress.stop()
                console.print(f"  [red]✗[/red] [dim]{label_prefix}{item} — {str(e)[:80]}[/dim]")

        if i < len(items):
            random_delay(*delay_range)

    return profiles


def _standard_export(profiles, total, platform_name, item_type="profiles"):
    """Shared post-scrape: summary, enrichment, CSV export."""
    if profiles:
        _success_summary(len(profiles), total, item_type)
        profiles = enrich_profiles(profiles)
        if Confirm.ask("[+] Export to CSV?", default=True):
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"{platform_name}_export_{timestamp}.csv"
            with open(filename, 'w', newline='', encoding='utf-8') as f:
                if profiles:
                    writer = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for p in profiles for k in p)), restval='')
                    writer.writeheader()
                    writer.writerows(profiles)
            _export_result(filename, len(profiles), item_type)
    else:
        _no_results()


def _collect_usernames(platform_name, prompt_label="Username", strip_at=False):
    """Shared username input loop. Returns list of usernames."""
    console.print(f"[white]Enter {platform_name} usernames (one per line)[/white]")
    console.print("[dim]Press Enter on empty line when done[/dim]")
    console.print()

    items = []
    while True:
        entry = Prompt.ask(prompt_label, default="")
        if not entry:
            break
        if strip_at:
            entry = entry.replace('@', '')
        entry = entry.strip()
        if entry:
            items.append(entry)
            prefix = "@" if strip_at else ""
            console.print(f"[green]✓[/green] Added {prefix}{entry}")

    if not items:
        console.print("[yellow]No usernames entered[/yellow]")
    return items


def scrape_instagram_interactive():
    _platform_header("Instagram")
    usernames = _collect_usernames("Instagram", strip_at=True)
    if not usernames:
        return
    console.print()
    profiles = _standard_scrape_loop(scrape_profile_no_login, usernames, label_prefix="@", delay_range=(1.5, 4.0))
    _standard_export(profiles, len(usernames), "instagram")


def scrape_tiktok_interactive():
    from app.scouting.tiktok import scrape_tiktok_profile
    _platform_header("TikTok")
    usernames = _collect_usernames("TikTok", strip_at=True)
    if not usernames:
        return
    console.print()
    profiles = _standard_scrape_loop(scrape_tiktok_profile, usernames, label_prefix="@", delay_range=(2.0, 5.0))
    _standard_export(profiles, len(usernames), "tiktok")


def scrape_linkedin_interactive():
    from app.scouting.linkedin import scrape_linkedin_profile

    cookie = os.environ.get('LINKEDIN_COOKIE', '').strip()
    if not cookie:
        console.print()
        console.print(Rule("[bold yellow]LinkedIn Cookie Required[/bold yellow]", style="yellow", align="left"))
        console.print()
        console.print("  [white]1.[/white] Open Chrome > linkedin.com (logged in)")
        console.print("  [white]2.[/white] F12 > Application > Cookies > linkedin.com")
        console.print("  [white]3.[/white] Copy the value of [bold]li_at[/bold]")
        console.print("  [white]4.[/white] Add to .env: [bold]LINKEDIN_COOKIE=your_value[/bold]")
        console.print("  [white]5.[/white] Restart Scout")
        console.print()
        return

    _platform_header("LinkedIn", "Using session cookie")

    console.print("[white]Enter LinkedIn usernames or profile URLs (one per line)[/white]")
    console.print("[dim]Press Enter on empty line when done[/dim]")
    console.print()

    usernames = []
    while True:
        entry = Prompt.ask("Username/URL", default="")
        if not entry:
            break
        entry = entry.strip().rstrip('/')
        if '/in/' in entry:
            entry = entry.split('/in/')[-1]
        entry = entry.lstrip('@').strip()
        if entry:
            usernames.append(entry)
            console.print(f"[green]✓[/green] Added {entry}")

    if not usernames:
        console.print("[yellow]No usernames entered[/yellow]")
        return

    console.print()
    profiles = _standard_scrape_loop(scrape_linkedin_profile, usernames, label_prefix="", delay_range=(3.0, 6.0))
    _standard_export(profiles, len(usernames), "linkedin")


def scrape_github_interactive():
    from app.scouting.github import scrape_profile
    _platform_header("GitHub")
    usernames = _collect_usernames("GitHub")
    if not usernames:
        return
    console.print()
    profiles = _standard_scrape_loop(scrape_profile, usernames, label_prefix="", delay_range=(0.5, 1.5))
    _standard_export(profiles, len(usernames), "github")


def scrape_youtube_interactive():
    from app.scouting.youtube import scrape_channel
    _platform_header("YouTube")

    console.print("[white]Enter YouTube channel handles (e.g. @MrBeast) or channel IDs[/white]")
    console.print("[dim]Press Enter on empty line when done[/dim]")
    console.print()

    channels = []
    while True:
        channel = Prompt.ask("Channel", default="")
        if not channel:
            break
        channel = channel.strip()
        if channel:
            channels.append(channel)
            console.print(f"[green]✓[/green] Added {channel}")

    if not channels:
        console.print("[yellow]No channels entered[/yellow]")
        return

    console.print()
    profiles = _standard_scrape_loop(scrape_channel, channels, label_prefix="", delay_range=(1.0, 2.5))
    _standard_export(profiles, len(channels), "youtube", "channels")


def scrape_twitch_interactive():
    from app.scouting.twitch import scrape_profile
    _platform_header("Twitch")
    usernames = _collect_usernames("Twitch")
    if not usernames:
        return
    console.print()
    profiles = _standard_scrape_loop(scrape_profile, usernames, label_prefix="", delay_range=(0.5, 1.5))
    _standard_export(profiles, len(usernames), "twitch")


def scrape_linktree_interactive():
    from app.scouting.linktree import scrape_linktree, scrape_all
    _platform_header("Link-in-Bio", "Linktree, Stan & more")

    console.print("[1] Linktree (linktr.ee)")
    console.print("[2] Auto-detect (try all)")
    console.print()
    platform_choice = Prompt.ask("[>] Platform", choices=["1", "2"], default="1")

    scraper_map = {"1": (scrape_linktree, "linktree"), "2": (scrape_all, "linkbio")}
    scraper_func, platform_name = scraper_map[platform_choice]

    console.print()
    usernames = _collect_usernames("link-in-bio")
    if not usernames:
        return

    console.print()
    profiles = _standard_scrape_loop(scraper_func, usernames, label_prefix="", delay_range=(0.5, 1.5))

    if profiles:
        _success_summary(len(profiles), len(usernames))
        profiles = enrich_profiles(profiles)
        if Confirm.ask("[+] Export to CSV?", default=True):
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"{platform_name}_export_{timestamp}.csv"

            export_profiles = []
            for p in profiles:
                flat = {k: v for k, v in p.items() if k not in ['links', 'socials']}
                if p.get('socials'):
                    for platform, handle in p['socials'].items():
                        flat[f'social_{platform}'] = handle
                export_profiles.append(flat)

            with open(filename, 'w', newline='', encoding='utf-8') as f:
                if export_profiles:
                    all_keys = set()
                    for p in export_profiles:
                        all_keys.update(p.keys())
                    writer = csv.DictWriter(f, fieldnames=sorted(all_keys))
                    writer.writeheader()
                    writer.writerows(export_profiles)
            _export_result(filename, len(profiles))
    else:
        _no_results()


def scrape_pinterest_interactive():
    from app.scouting.pinterest import scrape_profile
    _platform_header("Pinterest")
    usernames = _collect_usernames("Pinterest")
    if not usernames:
        return
    console.print()
    profiles = _standard_scrape_loop(scrape_profile, usernames, label_prefix="", delay_range=(1.0, 2.5))
    _standard_export(profiles, len(usernames), "pinterest")


def scrape_from_file():
    _platform_header("Bulk Scrape", "From username list file")
    console.print()

    filename = Prompt.ask("[>] Enter filename", default="usernames.txt")

    try:
        usernames = []
        if filename.lower().endswith('.csv'):
            with open(filename, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    username = row.get('username', '') or row.get('Username', '') or row.get('handle', '') or row.get('Handle', '')
                    if not username:
                        first_col = list(row.values())[0] if row else ''
                        username = first_col
                    username = username.strip().replace('@', '')
                    if username:
                        usernames.append(username)
        else:
            with open(filename, 'r', encoding='utf-8') as f:
                usernames = [line.strip().replace('@', '') for line in f if line.strip()]

        if not usernames:
            console.print(f"\n[red]✗ No usernames found in {filename}[/red]")
            return

        console.print(f"\n[white]Found {len(usernames)} usernames in {filename}[/white]")

        console.print()
        console.print(f"[bold {ACCENT}]Select platform:[/bold {ACCENT}]")
        console.print("[1] Instagram")
        console.print("[2] TikTok")
        console.print("[3] LinkedIn")
        console.print("[4] YouTube")
        console.print("[5] GitHub")
        console.print("[6] Twitch")
        console.print("[7] Linktree")
        console.print("[8] Pinterest")
        console.print()

        platform_choice = Prompt.ask("[>] Platform", choices=["1", "2", "3", "4", "5", "6", "7", "8"], default="1")

        platform_map = {
            "1": ("instagram", "Instagram"),
            "2": ("tiktok", "TikTok"),
            "3": ("linkedin", "LinkedIn"),
            "4": ("youtube", "YouTube"),
            "5": ("github", "GitHub"),
            "6": ("twitch", "Twitch"),
            "7": ("linktree", "Linktree"),
            "8": ("pinterest", "Pinterest"),
        }
        platform_key, platform_name = platform_map[platform_choice]

        if platform_key == "instagram":
            from app.scouting.instagram import scrape_profile_no_login as scraper_func
        elif platform_key == "tiktok":
            from app.scouting.tiktok import scrape_tiktok_profile as scraper_func
        elif platform_key == "linkedin":
            cookie = os.environ.get('LINKEDIN_COOKIE', '').strip()
            if not cookie:
                console.print("[red]✗ LINKEDIN_COOKIE not set in .env. Cannot bulk scrape LinkedIn.[/red]")
                return
            from app.scouting.linkedin import scrape_linkedin_profile as scraper_func
        elif platform_key == "youtube":
            from app.scouting.youtube import scrape_channel as scraper_func
        elif platform_key == "github":
            from app.scouting.github import scrape_profile as scraper_func
        elif platform_key == "twitch":
            from app.scouting.twitch import scrape_profile as scraper_func
        elif platform_key == "linktree":
            from app.scouting.linktree import scrape_linktree as scraper_func
        elif platform_key == "pinterest":
            from app.scouting.pinterest import scrape_profile as scraper_func

        console.print()
        console.print(f"[white]Scraping {len(usernames)} {platform_name} profiles...[/white]")

        if not Confirm.ask("Continue?", default=True):
            return

        console.print()

        profiles = []
        successful = 0

        for i, username in enumerate(usernames, 1):
            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console, transient=True) as progress:
                task = progress.add_task(f"[white]@{username}  [dim]({i}/{len(usernames)})[/dim]", total=None)

                try:
                    prev_level = logging.getLogger().level
                    if not _verbose:
                        logging.getLogger().setLevel(logging.CRITICAL)
                    profile = scraper_func(username)
                    if not _verbose:
                        logging.getLogger().setLevel(prev_level)

                    if profile:
                        profiles.append(profile)
                        successful += 1
                        follower_count = profile.get('follower_count', 0) or profile.get('subscribers', 0) or 0
                        progress.stop()
                        console.print(f"  [green]✓[/green] @{username}  [dim]{follower_count:,} followers[/dim]")
                    else:
                        progress.stop()
                        console.print(f"  [red]✗[/red] [dim]@{username}[/dim]")

                except Exception as e:
                    if not _verbose:
                        logging.getLogger().setLevel(prev_level)
                    progress.stop()
                    console.print(f"  [red]✗[/red] [dim]@{username}[/dim]")

            if i < len(usernames):
                random_delay(*_get_delay_range((1.5, 4.0)))

        console.print()

        if profiles:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            export_filename = f"{platform_key}_export_{timestamp}.csv"

            with open(export_filename, 'w', newline='', encoding='utf-8') as f:
                if profiles:
                    writer = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for p in profiles for k in p)), restval='')
                    writer.writeheader()
                    writer.writerows(profiles)

            _success_summary(successful, len(usernames))
            _export_result(export_filename, successful)
        else:
            _no_results()

    except FileNotFoundError:
        console.print(f"\n[red]✗ Error: File '{filename}' not found![/red]")
    except Exception as e:
        console.print(f"\n[red]✗ Error: {e}[/red]")


def discover_interactive():
    from app.scouting.discovery import discover, QUERY_TEMPLATES
    from app.enrichment.sources import search as web_search
    _platform_header("Discover", "Cross-border / recurring-payment businesses")
    cats = list(QUERY_TEMPLATES)
    for i, c in enumerate(cats, 1):
        console.print(f"  [{ACCENT}][{i}][/{ACCENT}] {c}")
    raw = Prompt.ask("[>] Categories (e.g. 1,3) or Enter for all", default="")
    picked = [cats[int(x) - 1] for x in raw.replace(' ', '').split(',') if x.isdigit() and 0 < int(x) <= len(cats)] or cats
    geo = Prompt.ask("[>] Region", default="India")
    per_q = int(Prompt.ask("[>] Results per search", default="10"))
    seed = None
    if not web_search.available():
        console.print("[yellow]No search API key set (SERPER_API_KEY / TAVILY_API_KEY / BRAVE_API_KEY); web search is often blocked. You can paste domains instead.[/yellow]")
        raw_seed = Prompt.ask("[>] Domains (comma-separated) or Enter to try search anyway", default="")
        seed = [d.strip() for d in raw_seed.split(',') if d.strip()] or None

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console, transient=True) as progress:
        task = progress.add_task("[white]Searching and analyzing sites...", total=None)
        leads = discover(picked, geo, per_q, seed_urls=seed, on_progress=lambda r: progress.update(task, description=f"[white]Analyzed {r[:50]}"))

    if not leads:
        return _no_results()
    t = Table(show_header=True, box=box.MINIMAL_HEAVY_HEAD, border_style="dim")
    for col in ("Company", "Fit", "Signals", "Email"):
        t.add_column(col)
    for l in leads[:25]:
        t.add_row(l['company'][:30], str(l['fit_score']), l['signals'].replace(',', ' '), l['email'] or '-')
    console.print(t)
    _success_summary(len(leads), len(leads), "businesses")
    if Confirm.ask("[+] Export to CSV?", default=True):
        filename = f"discovery_export_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        with open(filename, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for l in leads for k in l)), restval='')
            w.writeheader()
            w.writerows(leads)
        _export_result(filename, len(leads), "businesses")


def settings_menu():
    _platform_header("Settings", "Configure Scout")

    proxy = os.environ.get('SCOUT_PROXY', '')
    delay_min = os.environ.get('SCOUT_DELAY_MIN', '1.0')
    delay_max = os.environ.get('SCOUT_DELAY_MAX', '2.5')
    li_cookie = os.environ.get('LINKEDIN_COOKIE', '').strip()

    console.print(f"  [dim]Proxy:[/dim]     [green]{proxy[:40]}[/green]" if proxy else "  [dim]Proxy:[/dim]     [red]off[/red]")
    console.print(f"  [dim]Delay:[/dim]     [white]{delay_min}s - {delay_max}s[/white]")
    console.print(f"  [dim]LinkedIn:[/dim]  [green]cookie set[/green]" if li_cookie else "  [dim]LinkedIn:[/dim]  [dim]not configured[/dim]")
    console.print(f"  [dim]Scraped:[/dim]   [white]{_session_stats['scraped']}[/white] [dim]this session[/dim]")
    console.print()

    for n, label in (("1", "Set proxy URL"), ("2", "Test connection"), ("3", "Remove proxy"),
                     ("4", "Set scrape delay"), ("5", "Set LinkedIn cookie"), ("6", "Clear all exports"), ("0", "Back to menu")):
        console.print(f"  [{ACCENT}][{n}][/{ACCENT}]  {label}")
    console.print()

    choice = Prompt.ask(f"[{ACCENT}]>[/{ACCENT}]", choices=["0", "1", "2", "3", "4", "5", "6"], default="0")

    if choice == '1':
        url = Prompt.ask("[>] Proxy URL (e.g. http://user:pass@host:port)")
        if url.strip():
            _update_env('SCOUT_PROXY', url.strip())
            console.print("[green]✓ Proxy set[/green]")

    elif choice == '2':
        import httpx
        px = get_httpx_proxy()
        console.print(f"[white]Testing {'proxy ' + px[:60] if px else 'direct connection'}...[/white]")
        try:
            ip = httpx.get('https://httpbin.org/ip', proxy=px, timeout=10).json().get('origin', '?')
            console.print(f"[green]✓ Works. IP: {ip}[/green]")
        except Exception as e:
            console.print(f"[red]✗ Failed: {str(e)[:80]}[/red]")

    elif choice == '3':
        _update_env('SCOUT_PROXY', '')
        os.environ.pop('SCOUT_PROXY', None)
        console.print("[yellow]✓ Proxy removed[/yellow]")

    elif choice == '4':
        console.print(f"[dim]Current: {delay_min}s - {delay_max}s between requests[/dim]")
        new_min = Prompt.ask("[>] Min delay (seconds)", default=delay_min)
        new_max = Prompt.ask("[>] Max delay (seconds)", default=delay_max)
        try:
            fmin, fmax = float(new_min), float(new_max)
            if fmin < 0 or fmax < fmin:
                console.print("[red]✗ Invalid range[/red]")
            else:
                _update_env('SCOUT_DELAY_MIN', str(fmin))
                _update_env('SCOUT_DELAY_MAX', str(fmax))
                console.print(f"[green]✓ Delay set to {fmin}s - {fmax}s[/green]")
        except ValueError:
            console.print("[red]✗ Must be numbers[/red]")

    elif choice == '5':
        console.print("[white]Paste your LinkedIn li_at cookie value:[/white]")
        console.print("[dim]Chrome > F12 > Application > Cookies > linkedin.com > li_at[/dim]")
        cookie = Prompt.ask("[>] li_at", default="")
        if cookie.strip():
            _update_env('LINKEDIN_COOKIE', cookie.strip())
            console.print("[green]✓ LinkedIn cookie saved[/green]")

    elif choice == '6':
        csv_files = list(Path('.').glob('*_export_*.csv'))
        if not csv_files:
            console.print("[yellow]No exports to clear[/yellow]")
        else:
            console.print(f"[yellow]This will delete {len(csv_files)} export file(s)[/yellow]")
            if Confirm.ask("[>] Continue?", default=False):
                for f in csv_files:
                    f.unlink()
                console.print(f"[green]✓ Deleted {len(csv_files)} exports[/green]")


def view_exports():
    _platform_header("Exports", "Your scraped data")

    csv_files = list(Path('.').glob('*_export_*.csv'))

    if not csv_files:
        console.print("  [yellow]No exports found yet[/yellow]")
        console.print("  [dim]Run a scrape to create your first export[/dim]")
        return

    csv_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

    table = Table(box=box.MINIMAL_HEAVY_HEAD, border_style="dim", padding=(0, 1))
    table.add_column("#", style="dim", width=4)
    table.add_column("Filename", style="white")
    table.add_column("Size", style="white", justify="right")
    table.add_column("Date", style="dim")

    for i, file in enumerate(csv_files[:10], 1):
        size = file.stat().st_size / 1024
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(file.stat().st_mtime))
        table.add_row(str(i), file.name, f"{size:.1f} KB", mtime)

    console.print(table)
    console.print(f"  [dim]{len(csv_files)} exports total[/dim]")
    console.print()


def main():
    console.clear()
    show_header()

    while True:
        show_menu()

        try:
            choice = Prompt.ask(
                f"[{ACCENT}]>[/{ACCENT}]",
                choices=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"],
                default="1",
                show_choices=False
            )

            if choice == '0':
                console.clear()
                console.print()
                console.print(Rule(style=ACCENT_DIM))
                console.print(
                    f"  [bold {ACCENT}]Thanks for using Scout[/bold {ACCENT}]  "
                    "[dim]★ github.com/Tanv1n1/ScoutLeads[/dim]"
                )
                console.print()
                break

            console.clear()

            if choice == '1':
                scrape_instagram_interactive()
            elif choice == '2':
                scrape_tiktok_interactive()
            elif choice == '3':
                scrape_linkedin_interactive()
            elif choice == '4':
                scrape_github_interactive()
            elif choice == '5':
                scrape_youtube_interactive()
            elif choice == '6':
                scrape_twitch_interactive()
            elif choice == '7':
                scrape_linktree_interactive()
            elif choice == '8':
                scrape_pinterest_interactive()
            elif choice == '9':
                scrape_from_file()
            elif choice == '10':
                view_exports()
            elif choice == '11':
                settings_menu()
            elif choice == '12':
                discover_interactive()

            _pause()
            console.clear()
            show_header()

        except KeyboardInterrupt:
            console.print("\n\n[yellow]Exiting...[/yellow]")
            break
        except EOFError:
            console.print("\n[yellow]No more input, exiting.[/yellow]")
            break
        except Exception as e:
            console.print(f"\n[red]✗ Error: {e}[/red]")


if __name__ == '__main__':
    main()
