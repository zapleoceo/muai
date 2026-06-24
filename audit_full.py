#!/usr/bin/env python3
"""Full audit of 100 Stepan chats from production database"""

import json
import subprocess
import re
from collections import defaultdict

# Query the database and get JSON
result = subprocess.run(
    '''ssh -p 9617 hetzner-root "cd /var/www/stepan/infra && docker compose exec -T postgres psql -U stepan -d stepan -t -A -c \\\"
SELECT json_agg(
  json_build_object(
    'chat_id', c.id::text,
    'username', c.username,
    'product_slug', c.product_slug,
    'stage', c.stage,
    'agent_enabled', c.agent_enabled,
    'messages', array_agg(m.text ORDER BY m.occurred_at) FILTER (WHERE m.sent_by='agent' AND m.direction='out')
  )
)
FROM chats c
LEFT JOIN messages m ON c.id = m.chat_id
WHERE c.id IN (
  SELECT DISTINCT chat_id FROM messages
  WHERE direction='out' AND sent_by='agent'
  ORDER BY chat_id LIMIT 100
)
GROUP BY c.id, c.username, c.product_slug, c.stage, c.agent_enabled;
\\\"" 2>&1''',
    shell=True,
    capture_output=True,
    text=True,
    timeout=30
)

if result.returncode != 0:
    print(f"Database error: {result.stderr}")
    exit(1)

try:
    data = json.loads(result.stdout.strip())
except json.JSONDecodeError as e:
    print(f"JSON parse error: {e}")
    print(f"Output: {result.stdout[:500]}")
    exit(1)

# Known KB prices
PRICES_KB = {
    'vibe-coding': (12000000, 13000000),
    'python-backend': (12570000, 13360000),
    'cybersecurity': (20335950, 22595600),
    'smm-intensive': (1882955, 1882955),
}

def extract_prices(text):
    """Extract all prices from text"""
    if not text:
        return []
    prices = []
    # Match 1.234.567 or 1,234,567 or 1234567 patterns
    for match in re.finditer(r'\d{1,2}[\.,]\d{3}(?:[\.,]\d{3})*|\d+\s*(?:IDR|Rp)', text, re.IGNORECASE):
        price_str = match.group().replace('.', '').replace(',', '').replace('IDR', '').replace('Rp', '').strip()
        if price_str.isdigit():
            prices.append(int(price_str))
    return prices

def check_issues(chat):
    """Analyze single chat for issues"""
    issues = []
    chat_id = chat.get('chat_id')
    username = chat.get('username', 'unknown')
    product_slug = chat.get('product_slug', '')
    messages = chat.get('messages') or []

    if not messages:
        return issues

    full_text = ' '.join(messages)

    # Issue 1: Cyrillic characters (Russian text contamination)
    if re.search(r'[А-Яа-яЁё]', full_text):
        issues.append({
            'chat_id': chat_id,
            'username': username,
            'type': 'Language contamination - Cyrillic',
            'severity': 'high',
            'text': 'Russian text detected in chat',
            'fix': 'Remove non-Indonesian characters'
        })

    # Issue 2: Price validation
    prices = extract_prices(full_text)
    if prices:
        unique_prices = set(prices)
        if len(unique_prices) > 1:
            # Check if they're too different (>50% variation)
            min_p, max_p = min(unique_prices), max(unique_prices)
            if max_p / min_p > 1.5:
                issues.append({
                    'chat_id': chat_id,
                    'username': username,
                    'type': 'Price contamination',
                    'severity': 'high',
                    'text': f'Multiple prices: {sorted(unique_prices)}',
                    'fix': 'Verify single product; avoid mixing prices'
                })

        # Check vs KB
        for key, (min_kb, max_kb) in PRICES_KB.items():
            if key in (product_slug or '').lower() or key.replace('-', ' ') in full_text.lower():
                for price in prices:
                    if price < min_kb * 0.9 or price > max_kb * 1.1:
                        issues.append({
                            'chat_id': chat_id,
                            'username': username,
                            'type': 'Hallucinated price',
                            'severity': 'high',
                            'text': f'{price:,} IDR (KB: {min_kb:,}-{max_kb:,})',
                            'fix': f'Use correct price range for {key}'
                        })

    # Issue 3: Missing CTA in last message
    if messages and len(messages[-1]) > 50:
        cta_keywords = ['hubungi', 'daftar', 'link', 'klik', 'wa.me', 'whatsapp', 'amankan', 'tanya', 'pertanyaan']
        if not any(kw in messages[-1].lower() for kw in cta_keywords):
            issues.append({
                'chat_id': chat_id,
                'username': username,
                'type': 'Missing CTA',
                'severity': 'medium',
                'text': 'Conversation ends without call to action',
                'fix': 'End with clear next step (link, contact, booking)'
            })

    # Issue 4: Repetition detection
    if len(messages) >= 2:
        for i in range(len(messages) - 1):
            msg1 = messages[i].lower()
            msg2 = messages[i+1].lower()
            if len(msg1) > 40 and len(msg2) > 40:
                w1 = set(msg1.split())
                w2 = set(msg2.split())
                if len(w1) > 0 and len(w2) > 0:
                    overlap = len(w1 & w2) / max(len(w1), len(w2))
                    if overlap > 0.7:
                        issues.append({
                            'chat_id': chat_id,
                            'username': username,
                            'type': 'Duplicate content',
                            'severity': 'low',
                            'text': f'Messages {i+1}-{i+2} are {int(overlap*100)}% similar',
                            'fix': 'Vary response or consolidate'
                        })
                        break

    # Issue 5: Tone checks
    if any(word in full_text.lower() for word in ['dengan hormat', 'sebelumnya', 'adapun']):
        issues.append({
            'chat_id': chat_id,
            'username': username,
            'type': 'Tone - too formal',
            'severity': 'medium',
            'text': 'Formal language detected',
            'fix': 'Use warmer, casual Indonesian for Gen Z'
        })

    return issues

# Analyze all chats
all_issues = []
if isinstance(data, list):
    chats = data
else:
    chats = [data]

for chat in chats:
    if chat:
        all_issues.extend(check_issues(chat))

# Group and report
by_severity = defaultdict(list)
by_type = defaultdict(list)

for issue in all_issues:
    by_severity[issue['severity']].append(issue)
    by_type[issue['type']].append(issue)

print("\n" + "="*80)
print(f"STEPAN SALES BOT AUDIT: {len(chats)} chats analyzed")
print("="*80)

print(f"\nTotal issues found: {len(all_issues)}")
print(f"Affected chats: {len(set(i['chat_id'] for i in all_issues))}")

print("\nBY SEVERITY:")
for sev in ['high', 'medium', 'low']:
    issues = by_severity[sev]
    print(f"  {sev.upper()}: {len(issues)}")

print("\nBY TYPE:")
for typ in sorted(by_type.keys()):
    count = len(by_type[typ])
    print(f"  {typ}: {count}")

print("\n" + "="*80)
print("HIGH SEVERITY ISSUES (first 20)")
print("="*80)

for i, issue in enumerate(by_severity['high'][:20], 1):
    print(f"\n{i}. Chat #{issue['chat_id']} (@{issue['username']})")
    print(f"   Type: {issue['type']}")
    print(f"   Issue: {issue['text']}")
    print(f"   Fix: {issue['fix']}")

print("\n" + "="*80)
print("CSV EXPORT (paste into spreadsheet)")
print("="*80)
print("chat_id,username,issue_type,severity,issue_description,recommendation")

for issue in all_issues:
    desc = issue['text'].replace('"', "'").replace('\n', ' ')
    fix = issue['fix'].replace('"', "'")
    print(f'{issue["chat_id"]},{issue["username"]},"{issue["type"]}",{issue["severity"]},"{desc}","{fix}"')

print(f"\n\nTotal records: {len(all_issues)}")
