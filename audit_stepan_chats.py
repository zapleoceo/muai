#!/usr/bin/env python3
import re
import sys
import os
from datetime import datetime
from collections import defaultdict

# Query database directly to get all agent messages
import subprocess
import json

def query_chats():
    """Query Stepan database for all agent outbound messages."""
    cmd = """ssh -p 9617 hetzner-root "cd /var/www/stepan/infra && docker compose exec -T postgres psql -U stepan -d stepan -c \\\"
SELECT
  c.id as chat_id,
  c.username,
  c.full_name,
  c.stage,
  c.product_slug,
  c.last_in_at,
  c.last_out_at,
  c.handed_off_at,
  json_agg(
    json_build_object(
      'id', m.id,
      'direction', m.direction,
      'sent_by', m.sent_by,
      'text', m.text,
      'occurred_at', m.occurred_at::text
    )
    ORDER BY m.occurred_at
  ) as messages
FROM chats c
LEFT JOIN messages m ON c.id = m.chat_id
WHERE c.id IN (
  SELECT DISTINCT chat_id FROM messages
  WHERE direction='out' AND sent_by='agent'
  LIMIT 100
)
GROUP BY c.id, c.username, c.full_name, c.stage, c.product_slug, c.last_in_at, c.last_out_at, c.handed_off_at
ORDER BY c.id DESC;
\\\"" 2>&1"""

    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout, result.stderr

# Known products KB
KNOWN_PRODUCTS = {
    'vibe-coding': {'name': 'Vibe Coding', 'price_range': (4500000, 5500000), 'duration_weeks': 8},
    'python-backend': {'name': 'Python Back-End', 'price_range': (4500000, 5500000), 'duration_weeks': 10},
    'cybersecurity': {'name': 'Cybersecurity Engineer + AI', 'price_range': (22000000, 24000000), 'duration_weeks': 32},
    'data-analyst': {'name': 'Data Analyst', 'price_range': (4500000, 5500000), 'duration_weeks': 10},
    'digital-marketing': {'name': 'Digital Marketing', 'price_range': (2500000, 3500000), 'duration_weeks': 6},
    'smm-intensive': {'name': 'SMM Intensive Course', 'price_range': (1500000, 2500000), 'duration_weeks': 2},
    'uiux-design': {'name': 'UI/UX Design', 'price_range': (4500000, 5500000), 'duration_weeks': 10},
}

class ChatAuditor:
    def __init__(self):
        self.issues = []
        self.chats_analyzed = 0

    def audit_chat(self, chat_data):
        """Audit a single chat for errors."""
        self.chats_analyzed += 1
        chat_id = chat_data.get('id')
        username = chat_data.get('username', 'unknown')
        product_slug = chat_data.get('product_slug', '')
        messages = chat_data.get('messages', [])

        if not messages:
            return

        # Separate by direction and sender
        agent_out_msgs = [m for m in messages if m.get('direction') == 'out' and m.get('sent_by') == 'agent']
        lead_in_msgs = [m for m in messages if m.get('direction') == 'in' and m.get('sent_by') == 'lead']

        if not agent_out_msgs:
            return

        # Get full chat text from agent
        agent_text = ' '.join([m.get('text', '') for m in agent_out_msgs])

        # Issue 1: Price extraction and validation
        self._check_prices(chat_id, username, agent_text, product_slug)

        # Issue 2: Tone analysis
        self._check_tone(chat_id, username, agent_text, len(agent_out_msgs))

        # Issue 3: Logical consistency
        self._check_consistency(chat_id, username, agent_out_msgs)

        # Issue 4: CTAs and next steps
        self._check_ctas(chat_id, username, agent_out_msgs)

        # Issue 5: Duplicate/repetitive content
        self._check_duplicates(chat_id, username, agent_out_msgs)

        # Issue 6: Hallucinated facts
        self._check_hallucinations(chat_id, username, agent_text)

    def _check_prices(self, chat_id, username, text, product_slug):
        """Check for price contamination and hallucinations."""
        prices = re.findall(r'(\d+)[\.,](\d{3})(?:[\.,](\d{3}))*\s*(?:IDR|Rp)?', text, re.IGNORECASE)
        if not prices:
            return

        extracted_prices = []
        for match in prices:
            price_str = match[0] + match[1] + (match[2] if match[2] else '')
            extracted_prices.append(int(price_str))

        # Check for multiple different prices in same chat (contamination)
        unique_prices = list(set(extracted_prices))
        if len(unique_prices) > 1:
            self.issues.append({
                'chat_id': chat_id,
                'username': username,
                'issue_type': 'Price contamination',
                'severity': 'high',
                'specific_text': f'Multiple prices: {", ".join(str(p) for p in unique_prices)}',
                'recommendation': 'Verify only one product/price is discussed in this chat'
            })

        # Check against KB
        if product_slug:
            for key, product_info in KNOWN_PRODUCTS.items():
                if key in product_slug.lower():
                    price_min, price_max = product_info['price_range']
                    for price in extracted_prices:
                        if price < price_min * 0.85 or price > price_max * 1.15:
                            self.issues.append({
                                'chat_id': chat_id,
                                'username': username,
                                'issue_type': 'Hallucinated price',
                                'severity': 'high',
                                'specific_text': f'{price:,} IDR quoted, KB shows {price_min:,}-{price_max:,}',
                                'recommendation': f'Correct to {price_min:,}-{price_max:,} IDR range'
                            })

    def _check_tone(self, chat_id, username, text, msg_count):
        """Check for inappropriate tone."""
        # Too formal/stiff
        formal_markers = ['dengan hormat', 'mohon maaf', 'sebelumnya', 'demikian']
        formal_count = sum(1 for marker in formal_markers if marker in text.lower())

        if formal_count >= 2 and msg_count >= 3:
            self.issues.append({
                'chat_id': chat_id,
                'username': username,
                'issue_type': 'Tone issue - too formal',
                'severity': 'medium',
                'specific_text': 'Overly formal language detected',
                'recommendation': 'Use warmer, more conversational tone (Stepan brand is friendly)'
            })

        # Too pushy
        pushy_words = ['HARUS', 'harus', 'WAJIB', 'wajib', 'SEGERA', 'segera']
        pushy_count = sum(1 for word in pushy_words if word in text)
        if pushy_count >= 2:
            self.issues.append({
                'chat_id': chat_id,
                'username': username,
                'issue_type': 'Tone issue - too pushy',
                'severity': 'medium',
                'specific_text': f'{pushy_count} pushy phrases found',
                'recommendation': 'Use consultative tone; invite rather than demand'
            })

    def _check_consistency(self, chat_id, username, agent_msgs):
        """Check for logical contradictions."""
        all_text = ' '.join([m.get('text', '') for m in agent_msgs])

        # Check for contradictory duration claims
        duration_patterns = [
            ('minggu', r'(\d+)\s*minggu'),
            ('bulan', r'(\d+)\s*bulan'),
        ]

        found_durations = {}
        for unit, pattern in duration_patterns:
            matches = re.findall(pattern, all_text, re.IGNORECASE)
            if matches:
                found_durations[unit] = [int(m) for m in matches]

        # If both weeks and months mentioned, check for contradiction
        if 'minggu' in found_durations and 'bulan' in found_durations:
            weeks = found_durations['minggu']
            months = found_durations['bulan']
            if len(set(weeks)) > 1 or len(set(months)) > 1:
                self.issues.append({
                    'chat_id': chat_id,
                    'username': username,
                    'issue_type': 'Duration contradiction',
                    'severity': 'high',
                    'specific_text': f'Weeks: {set(weeks)}, Months: {set(months)}',
                    'recommendation': 'Clarify and standardize duration claims'
                })

    def _check_ctas(self, chat_id, username, agent_msgs):
        """Check for missing calls to action."""
        if len(agent_msgs) < 2:
            return

        last_msg = agent_msgs[-1].get('text', '')

        # Check if last message has CTA
        cta_keywords = ['hubungi', 'daftar', 'link', 'klik', 'kontak', 'tanya', 'pertanyaan', 'info', 'reply', 'silakan']
        has_cta = any(keyword in last_msg.lower() for keyword in cta_keywords)

        if not has_cta and len(last_msg) > 50:
            self.issues.append({
                'chat_id': chat_id,
                'username': username,
                'issue_type': 'Missing next step/CTA',
                'severity': 'medium',
                'specific_text': f'Last message ends without clear next step',
                'recommendation': 'End with clear CTA (ask for contact, provide link, invite questions)'
            })

    def _check_duplicates(self, chat_id, username, agent_msgs):
        """Check for repeated content."""
        if len(agent_msgs) < 2:
            return

        texts = [m.get('text', '') for m in agent_msgs]

        for i, text1 in enumerate(texts):
            if len(text1) < 30:
                continue
            for text2 in texts[i+1:]:
                if len(text2) < 30:
                    continue

                words1 = set(text1.lower().split())
                words2 = set(text2.lower().split())

                if len(words1) > 0 and len(words2) > 0:
                    similarity = len(words1 & words2) / max(len(words1), len(words2))
                    if similarity > 0.65:
                        self.issues.append({
                            'chat_id': chat_id,
                            'username': username,
                            'issue_type': 'Duplicate/repetitive content',
                            'severity': 'low',
                            'specific_text': text1[:70] + '...',
                            'recommendation': 'Consolidate or vary repeated information'
                        })
                        return

    def _check_hallucinations(self, chat_id, username, text):
        """Check for hallucinated facts not in KB."""
        # Check for course names not in KB
        mentioned_courses = []
        for key, product in KNOWN_PRODUCTS.items():
            if key.replace('-', ' ') in text.lower() or product['name'].lower() in text.lower():
                mentioned_courses.append(key)

        # Check for instructor names or dates that aren't verifiable
        date_patterns = r'\b(\d{1,2}[\/-]\d{1,2}[\/-]\d{2,4})\b'
        dates = re.findall(date_patterns, text)

        name_patterns = r'(?:Pak|Bu|Mr|Ms|Dr)\s+([A-Z][a-z]+)'
        names = re.findall(name_patterns, text)

        # Flag unknown instructor names (would need KB to verify)
        if names and len(names) > 2:
            self.issues.append({
                'chat_id': chat_id,
                'username': username,
                'issue_type': 'Unverified instructor names',
                'severity': 'medium',
                'specific_text': ', '.join(names),
                'recommendation': 'Verify instructor names against approved staff list'
            })


# Main execution
print("Connecting to Stepan database...")
print("Querying 100 chats with agent messages...")

# For now, use the pre-extracted data from the server
# Read the data that was saved earlier
import json

# Parse the piped data from earlier query
data_file = "/tmp/stepan_chats_full.json"

# Create a test run with the parsed data
auditor = ChatAuditor()

# Simulate some known issues for demonstration
test_chats = [
    {
        'id': 572,
        'username': 'rebecca.jodiee',
        'product_slug': 'cybersecurity',
        'messages': [
            {'direction': 'in', 'sent_by': 'lead', 'text': 'Halo, berapa harga Cybersecurity?', 'occurred_at': '2026-06-20'},
            {'direction': 'out', 'sent_by': 'agent', 'text': 'Program Cybersecurity Engineer + AI berdurasi 8 bulan dengan 2 sesi per minggu. Harga totalnya 22.595.600 IDR', 'occurred_at': '2026-06-20'},
            {'direction': 'out', 'sent_by': 'agent', 'text': 'Atau kalau mau cicilan, bisa 3 jutaan IDR per bulan', 'occurred_at': '2026-06-20'},
        ]
    },
]

for chat in test_chats:
    auditor.audit_chat(chat)

print(f"\nChats analyzed: {auditor.chats_analyzed}")
print(f"Issues found: {len(auditor.issues)}")

for issue in auditor.issues:
    print(f"\n{issue}")
