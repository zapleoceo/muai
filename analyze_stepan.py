#!/usr/bin/env python3
"""
Comprehensive audit of Stepan Instagram sales bot chats.
Analyzes agent-sent messages for errors, hallucinations, and tone issues.
"""

import re
from collections import defaultdict
from typing import List, Dict, Tuple

# Known product KB (from STRATEGY-2026-06-15.md and official materials)
KNOWN_PRODUCTS = {
    'vibe-coding': {
        'name': 'Vibe Coding',
        'price_range': (12000000, 13000000),  # 12-13 jt IDR
        'duration': '~4 bulan',
        'sessions': '37 sesi',
        'frequency': '2x seminggu',
        'dp': 100000,
        'discount_price': 12000000,  # with 1jt discount
    },
    'python-backend': {
        'name': 'Python Back-End Developer + AI',
        'price_range': (12570000, 13360000),  # 12.57-13.36 jt
        'duration': '8 bulan',
        'sessions': '46 modul',
        'frequency': '2x seminggu',
        'dp': 100000,
        'monthly': 1670000,
    },
    'cybersecurity': {
        'name': 'Cybersecurity Engineer + AI',
        'price_range': (20335950, 22595600),  # 20.3-22.6 jt
        'duration': '8 bulan',
        'frequency': '2x seminggu',
        'dp': 100000,
        'monthly': 2824450,
    },
    'data-analyst': {
        'name': 'Data Analyst',
        'price_range': (4000000, 5500000),
        'duration': '10-12 minggu',
    },
    'digital-marketing': {
        'name': 'Digital Marketing',
        'price_range': (2500000, 3500000),
        'duration': '4-6 minggu',
    },
    'smm-intensive': {
        'name': 'SMM Intensive Course',
        'price_range': (1882955, 1882955),  # Fixed price
        'duration': '2 minggu',
        'frequency': '3x per minggu online + 1x kantor',
        'dp': 100000,
    },
    'uiux-design': {
        'name': 'UI/UX Design',
        'price_range': (4000000, 5500000),
        'duration': '10-12 minggu',
    },
}

# Known instructor patterns (should verify against staff list)
KNOWN_FEATURES = {
    'cisco': 'Cisco CyberOps Associate certification',
    'menara sudirman': 'Official location',
    'teams': 'Microsoft Teams platform',
    'sertifikat': 'IT STEP certification',
}

# Problematic patterns
SUSPICIOUS_PATTERNS = {
    'russian_phone': r'Телефон.*\+\d+',  # Cyrillic characters
    'multiple_prices': None,  # Checked programmatically
    'vague_promises': [
        r'pasti.*kerja',
        r'100%.*diterima',
        r'garansi.*pekerjaan',
    ],
}

class ChatAuditor:
    def __init__(self):
        self.issues = []
        self.chats_analyzed = 0
        self.total_msgs = 0

    def extract_prices(self, text: str) -> List[int]:
        """Extract all prices in IDR from text."""
        # Match patterns like: 1.670.000, 1,670,000, 1670000 IDR
        pattern = r'(\d{1,2})\.(\d{3})(?:\.(\d{3}))*|(\d{1,2}),(\d{3})(?:,(\d{3}))*|Rp[\s]?(\d+(?:[.,]\d{3})*)'
        matches = re.findall(pattern, text, re.IGNORECASE)

        prices = []
        for match in matches:
            # Reconstruct the full number
            if match[0]:  # Period-separated format
                price_str = match[0] + match[1] + (match[2] if match[2] else '')
            elif match[3]:  # Comma-separated format
                price_str = match[3] + match[4] + (match[5] if match[5] else '')
            elif match[6]:  # Rp format
                price_str = match[6].replace(',', '').replace('.', '')
            else:
                continue

            try:
                prices.append(int(price_str))
            except ValueError:
                pass

        return prices

    def audit_chat(self, chat_id: str, username: str, product_slug: str, messages: List[str]):
        """Audit a single chat."""
        if not messages:
            return

        self.chats_analyzed += 1
        self.total_msgs += len(messages)

        # Concatenate all agent messages
        full_text = ' '.join(messages)

        # 1. Check price accuracy
        self._check_prices(chat_id, username, full_text, product_slug)

        # 2. Check for tone issues
        self._check_tone(chat_id, username, full_text, len(messages))

        # 3. Check for hallucinations (unverifiable facts)
        self._check_hallucinations(chat_id, username, full_text)

        # 4. Check for logical consistency
        self._check_consistency(chat_id, username, full_text)

        # 5. Check for CTAs and next steps
        self._check_ctas(chat_id, username, messages)

        # 6. Check for repetition/duplication
        self._check_repetition(chat_id, username, messages)

        # 7. Check for culture fit (warmth, appropriateness)
        self._check_culture(chat_id, username, full_text)

    def _check_prices(self, chat_id: str, username: str, text: str, product_slug: str):
        """Validate prices against KB."""
        prices = self.extract_prices(text)

        if not prices:
            return

        # Flag: Multiple different prices in same chat
        unique_prices = list(set(prices))
        if len(unique_prices) > 1:
            # Check if they're alternative payment plans for SAME product
            price_strs = [f'{p:,}' for p in unique_prices]

            # Allow variations if they're clearly payment options (monthly vs lump sum)
            # But flag if dramatically different prices for different products are mixed
            min_price, max_price = min(unique_prices), max(unique_prices)
            ratio = max_price / min_price if min_price > 0 else 0

            if ratio > 1.5:  # More than 50% variation suggests contamination
                self.issues.append({
                    'chat_id': chat_id,
                    'username': username,
                    'issue_type': 'Price contamination',
                    'severity': 'high',
                    'specific_text': ' | '.join(price_strs),
                    'recommendation': 'Verify single product discussion; may be mixing different courses'
                })

        # Flag: Prices outside KB range
        for product_key, product_info in KNOWN_PRODUCTS.items():
            if product_key.replace('-', ' ') in text.lower() or product_key in (product_slug or '').lower():
                price_min, price_max = product_info['price_range']

                for price in prices:
                    # Allow 10% tolerance for seasonal variations
                    if price < price_min * 0.9 or price > price_max * 1.1:
                        self.issues.append({
                            'chat_id': chat_id,
                            'username': username,
                            'issue_type': 'Hallucinated/incorrect price',
                            'severity': 'high',
                            'specific_text': f'{price:,} IDR for {product_key}',
                            'recommendation': f'KB shows {price_min:,}-{price_max:,} IDR for {product_key}'
                        })
                        break

    def _check_tone(self, chat_id: str, username: str, text: str, msg_count: int):
        """Check for inappropriate tone."""
        # Too formal
        formal_markers = ['dengan hormat', 'sebelumnya', 'adapun', 'demikian']
        formal_count = sum(1 for marker in formal_markers if marker in text.lower())

        if formal_count >= 2:
            self.issues.append({
                'chat_id': chat_id,
                'username': username,
                'issue_type': 'Tone - overly formal',
                'severity': 'medium',
                'specific_text': f'Found {formal_count} formal markers in {msg_count} messages',
                'recommendation': 'Use warmer, conversational Indonesian tone (Stepan brand is friendly, not stiff)'
            })

        # Too pushy/aggressive
        pushy_words = ['HARUS', 'WAJIB', 'SEGERA', 'JANGAN TUNDA', 'harus ambil keputusan sekarang']
        pushy_count = sum(1 for word in pushy_words if word.lower() in text.lower())

        if pushy_count >= 2:
            self.issues.append({
                'chat_id': chat_id,
                'username': username,
                'issue_type': 'Tone - too pushy',
                'severity': 'medium',
                'specific_text': f'{pushy_count} pushy directives detected',
                'recommendation': 'Use consultative tone; invite rather than pressure'
            })

    def _check_hallucinations(self, chat_id: str, username: str, text: str):
        """Check for unverifiable or fabricated facts."""
        # Check for Russian language (must be Indonesian only)
        if re.search(r'[А-Яа-яЁё]', text):
            self.issues.append({
                'chat_id': chat_id,
                'username': username,
                'issue_type': 'Language contamination - Cyrillic found',
                'severity': 'high',
                'specific_text': 'Russian text mixed into Indonesian chat',
                'recommendation': 'Remove all non-Indonesian text; verify message accuracy'
            })

        # Check for unverifiable instructor names (too many different names suggests fabrication)
        name_pattern = r'(?:Pak|Bu|Mr|Ms|Dr\.?)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)'
        names = re.findall(name_pattern, text)

        if len(names) > 3:
            self.issues.append({
                'chat_id': chat_id,
                'username': username,
                'issue_type': 'Unverified facts - excessive instructor references',
                'severity': 'medium',
                'specific_text': f'Multiple instructor names: {", ".join(set(names))}',
                'recommendation': 'Verify instructor names against approved staff list'
            })

        # Check for specific date claims outside of known batch dates
        date_pattern = r'\d{1,2}\s+(?:Juli|Agustus|September|Oktober|November|Desember)\s+2026'
        dates = re.findall(date_pattern, text)
        known_dates = ['11 Juli 2026', '29 Juni 2026']  # Open house and Vibe Coding start date

        for date in dates:
            if date not in known_dates and 'Juli' not in date:  # July programs are confirmed
                self.issues.append({
                    'chat_id': chat_id,
                    'username': username,
                    'issue_type': 'Unverified date claim',
                    'severity': 'low',
                    'specific_text': f'Date mentioned: {date}',
                    'recommendation': 'Verify batch start dates against official schedule'
                })

    def _check_consistency(self, chat_id: str, username: str, text: str):
        """Check for logical contradictions."""
        # Duration contradictions
        duration_patterns = {
            'weeks': r'(\d+)\s*minggu',
            'months': r'(\d+)\s*bulan',
            'days': r'(\d+)\s*(?:hari|days)',
        }

        found = {}
        for unit, pattern in duration_patterns.items():
            matches = [int(m) for m in re.findall(pattern, text, re.IGNORECASE)]
            if matches:
                found[unit] = matches

        # If mixing weeks and months, check for math contradiction
        if 'weeks' in found and 'months' in found:
            weeks_list = found['weeks']
            months_list = found['months']

            # Check if they're describing the same program with contradictory durations
            if len(weeks_list) > 1 or len(months_list) > 1:
                self.issues.append({
                    'chat_id': chat_id,
                    'username': username,
                    'issue_type': 'Duration contradiction',
                    'severity': 'high',
                    'specific_text': f'Weeks: {set(weeks_list)}, Months: {set(months_list)}',
                    'recommendation': 'Standardize duration claims; verify against product spec'
                })

    def _check_ctas(self, chat_id: str, username: str, messages: List[str]):
        """Check for missing call-to-action."""
        if len(messages) < 2:
            return

        last_msg = messages[-1].lower() if messages else ''

        # Check if last message has clear next step
        cta_keywords = ['hubungi', 'daftar', 'link', 'klik', 'whatsapp', 'kontak', 'tanya', 'pertanyaan', 'info lebih', 'amankan', 'slot', 'open house', 'wa.me']

        has_cta = any(keyword in last_msg for keyword in cta_keywords)

        if not has_cta and len(messages[-1]) > 50:
            self.issues.append({
                'chat_id': chat_id,
                'username': username,
                'issue_type': 'Missing CTA/next step',
                'severity': 'medium',
                'specific_text': 'Conversation ends without clear call to action',
                'recommendation': 'End with: ask for contact, provide link, invite questions, or offer to book'
            })

    def _check_repetition(self, chat_id: str, username: str, messages: List[str]):
        """Check for repetitive content."""
        if len(messages) < 3:
            return

        # Compare message pairs
        for i, msg1 in enumerate(messages):
            if len(msg1) < 40:
                continue

            for msg2 in messages[i+1:]:
                if len(msg2) < 40:
                    continue

                words1 = set(msg1.lower().split())
                words2 = set(msg2.lower().split())

                if len(words1) > 0 and len(words2) > 0:
                    overlap = len(words1 & words2)
                    union = len(words1 | words2)
                    jaccard = overlap / union if union > 0 else 0

                    if jaccard > 0.65:  # High similarity
                        self.issues.append({
                            'chat_id': chat_id,
                            'username': username,
                            'issue_type': 'Repetitive content',
                            'severity': 'low',
                            'specific_text': msg1[:60] + '...',
                            'recommendation': 'Vary message content or consolidate repeated information'
                        })
                        return

    def _check_culture(self, chat_id: str, username: str, text: str):
        """Check brand/cultural fit."""
        # Check for emojis (Stepan brand uses them warmly)
        emoji_count = len(re.findall(r'[😊🙏😄👍❤️💡🚀🎯]', text))

        # Count formal greetings vs warm greetings
        warm_greetings = len(re.findall(r'(?:hai|halo|kak|yuk|asik|asyik|mantap)', text, re.IGNORECASE))
        formal_greetings = len(re.findall(r'(?:assalamu|dengan hormat|perihal)', text, re.IGNORECASE))

        # Indonesian should be casual/warm for B2C messaging to Gen Z
        if formal_greetings > warm_greetings:
            self.issues.append({
                'chat_id': chat_id,
                'username': username,
                'issue_type': 'Culture mismatch - too formal/corporate',
                'severity': 'medium',
                'specific_text': 'Formal tone detected in Gen Z sales channel',
                'recommendation': 'Use casual, warm Indonesian; typical: "Hai Kak!", "Yuk coba", "Asik banget"'
            })


def main():
    # Sample data extracted from the database query
    sample_chats = [
        ('573', 'noviannugraha22', '', [
            'Vibe Coding adalah program 4 bulan yang mengajarkan cara bikin dan launch aplikasi AI dari nol, dengan kelas kecil ≤14 orang. Kelas dimulai 11 Juli 2026, dua kali seminggu (setiap malam) – pilih grup A (Senin & Kamis), B (Selasa & Jumat), atau C (Sabtu & Minggu). Harga reguler 13 jt IDR (bisa dicicil 4 × 3,250,000 per bulan) atau DP 100 rb + book‑now discount 1 jt jadi 12 jt IDR (4 × 3,000,000 per bulan). Di akhir kelas Kakak bakal punya aplikasi full‑stack yang terdeploy, portfolio, serta sertifikat internasional IT STEP.'
        ]),
        ('572', 'rebecca.jodiee', '', [
            'Program Cybersecurity Engineer + AI berdurasi 8 bulan dengan 2 sesi per minggu, fokus pada hands‑on lab SOC, jaringan, dan persiapan sertifikasi Cisco CyberOps Associate. Harga totalnya 22.595.600 IDR (bisa dicicil 8 bulan × 2.824.450 IDR per bulan) atau 21.465.700 IDR per semester, atau 20.335.950 IDR jika bayar penuh di muka – semua tanpa bunga. Anda bisa amankan seat dengan DP 100.000 IDR dan sisanya dibayar sebelum kelas dimulai; kelas offline di Menara Sudirman atau online via Teams. Kak ada goal khusus atau ingin amankan seat dengan DP sekarang?',
            '📱 Телефон · +62 22 595600'
        ]),
        ('571', 'angyiibs9y', '', [
            'Untuk SMM Intensive Course, biayanya 1.882.955 IDR. Programnya 2 minggu, dengan 3 sesi per minggu secara online + 1 kunjungan kantor tiap minggu untuk filming konten. Di akhir kursus, Kakak bisa bikin konten, mengelola iklan Meta, dan menjalankan kampanye sosial media secara mandiri. Mau saya kirimkan link landing page atau brosur lengkapnya? 😊',
            'Berikut link landing page dan brosur SMM Intensive Course: 🔗 https://itstep.id/smm-intensive-course 📄 https://drive.google.com/file/d/1FhhLFo-jx_EYNGG1HIzfO3ugiXYZuMbX/view Dengan program intensif 2 minggu ini, Kakak bakal bisa bikin konten, kelola iklan Meta, dan jalankan kampanye sosial media secara mandiri. Ada yang ingin Kakak tanyakan lebih lanjut?',
            'Baik Kak, terima kasih sudah menghubungi kami 🙏 Kalau ada pertanyaan lagi atau ingin info lebih lanjut, silakan hubungi kapan saja ya 😊',
            'Baik Kak, terima kasih ya 🙏 Jika nanti ada pertanyaan atau butuh info tambahan, silakan hubungi kapan saja, kami siap membantu 😊'
        ]),
        ('567', 'acha_fachrina', '', [
            'Halo Kak! Selamat datang 😊 Program Python Back-End Developer + AI cocok buat yang ingin menjadi developer back‑end profesional sekaligus menguasai AI. Kakak ada tujuan khusus, misalnya ingin membangun aplikasi tertentu atau beralih karier ke bidang back‑end?',
            'Untuk Python Back-End Developer + AI, investasinya 13.360.000 IDR kalau cicilan 4×3.250.000 per bulan (interest‑free), atau 12.960.000 per semester, atau 12.570.000 kalau bayar sekaligus. Dalam 8 bulan kamu bakal menguasai Python, Django, REST API, serta AI tools, dapat portfolio project nyata, dan sertifikat internasional. Kakak mau saya kirim detail lengkapnya atau amankan seat dengan DP 100rb dulu?',
            '1.670.000 adalah biaya per bulannya ya kak',
            'Flat selama 8 bulan',
            'Kita juga ada kelas Skill Booster untuk Python Development besok pagi yaitu kelas pengenalan yang berdurasi 1 hari atau 5 jam',
            '1.670.000 IDR per bulan selama 8 bulan, totalnya 13.360.000 IDR (atau 12.960.000 per semester, 12.570.000 kalau bayar sekaligus). Di program ini kamu bakal menguasai Python, Django, REST API, serta AI tools, dapat portfolio project nyata, dan sertifikat internasional. Mau aku kirim detail lengkap (silabus) atau amankan seat dengan DP 100rb dulu?',
            'Mungkin kakak mau ikut?',
            'Kak, dengan DP 100 rb seat bisa di‑amankan dan kalau reservasi dalam 24 jam dapat potongan 1 jt, jadi totalnya hanya 12 jt untuk 8 bulan belajar Python, Django, AI plus portfolio proyek nyata. Ada project atau target khusus yang ingin Kakak capai dengan Python?'
        ]),
        ('549', 'setotino', '', [
            'Program Cybersecurity Engineer + AI berdurasi 8 bulan (120 jam), 2 sesi/minggu, bisa diikuti offline di Menara Sudirman atau online via Teams. Kurikulum meliputi dasar keamanan, OS Windows & Linux, jaringan, protokol, threat intel, kriptografi, vulnerability assessment, forensik digital, hingga persiapan sertifikasi Cisco CyberOps Associate. Harga total 22.595.600 IDR bila cicilan 4×2.824.450 per bulan, atau 20.335.950 IDR bila bayar penuh (ada pilihan semester 10.732.850 per semester). DP 100.000 IDR untuk amankan seat, sisanya dibayar sebelum kelas dimulai. Lulus nanti Kakak dapat sertifikat Cisco CyberOps Associate plus portofolio proyek SOC yang siap pakai. Kakak ingin fokus pada karir keamanan atau ingin memperkuat CV? Atau mau aku kirimkan brosur dan link pendaftaran?',
            '📱 Телефон · +62 811-1314-400',
            'Nomor WhatsApp untuk informasi lebih lanjut: +62 811 1314 400 atau https://wa.me/+6281111858519 🙏',
            '📱 Телефон · +62 811-1314-400',
            'Kak, dalam 8 bulan ke depan kamu bakal dapat sertifikat Cisco CyberOps Associate plus portofolio proyek SOC yang siap pakai—bisa langsung memperkuat CV. Kalau ingin amankan tempat, cukup DP 100 rb, sisanya dapat dibayar nanti sebelum kelas dimulai.',
            'Betul Kak, program Cybersecurity Engineer + AI ini adalah bagian resmi dari Cisco Networking Academy yang mempersiapkan untuk sertifikasi Cisco CyberOps Associate. Walaupun Kakak sudah punya sertifikasi, kelas ini tetap memberi banyak praktik lab SOC, proyek portofolio, dan skill mendalam yang bakal memperkuat pengalaman kerja Kakak. Mau aku kirimkan brosur lengkapnya atau bantu amankan seat dengan DP 100 rb?',
            'Kak, kelas mulai 11 Juli dan masih ada beberapa seat kosong, jadi cukup DP 100 rb untuk amankan tempat, sisanya bisa dibayar nanti. Selain itu, kamu bakal dibimbing langsung oleh mentor yang pernah kerja di Cisco, jadi proyek SOC kamu jadi nyata dan siap pakai. 🙏'
        ]),
    ]

    auditor = ChatAuditor()

    for chat_id, username, product_slug, messages in sample_chats:
        auditor.audit_chat(chat_id, username, product_slug, messages)

    # Print report
    print("=" * 80)
    print("STEPAN INSTAGRAM SALES BOT - CHAT AUDIT REPORT")
    print("=" * 80)
    print(f"\nChats Analyzed: {auditor.chats_analyzed}")
    print(f"Total Messages: {auditor.total_msgs}")
    print(f"Total Issues Found: {len(auditor.issues)}\n")

    # Group by severity
    severity_groups = defaultdict(list)
    type_groups = defaultdict(list)

    for issue in auditor.issues:
        severity_groups[issue['severity']].append(issue)
        type_groups[issue['issue_type']].append(issue)

    # Print by severity
    print("ISSUES BY SEVERITY")
    print("-" * 80)
    for severity in ['high', 'medium', 'low']:
        issues = severity_groups[severity]
        print(f"\n{severity.upper()}: {len(issues)} issues")
        for i, issue in enumerate(issues[:5], 1):
            print(f"\n  {i}. Chat #{issue['chat_id']} (@{issue['username']}) - {issue['issue_type']}")
            print(f"     Issue: {issue['specific_text'][:75]}")
            print(f"     Action: {issue['recommendation'][:75]}")

    # Print summary by type
    print("\n\nISSUES BY TYPE")
    print("-" * 80)
    for issue_type in sorted(type_groups.keys()):
        issues = type_groups[issue_type]
        print(f"{issue_type}: {len(issues)}")

    # Export CSV format for inspection
    print("\n\nDETAILED CSV FORMAT (for spreadsheet import)")
    print("-" * 80)
    print("chat_id,username,issue_type,severity,specific_text,recommendation")
    for issue in auditor.issues:
        text = issue['specific_text'].replace(',', ';').replace('"', '')
        rec = issue['recommendation'].replace(',', ';').replace('"', '')
        print(f'{issue["chat_id"]},{issue["username"]},"{issue["issue_type"]}",{issue["severity"]},"{text}","{rec}"')


if __name__ == '__main__':
    main()
