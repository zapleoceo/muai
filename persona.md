# Sales Lead Persona & Ready States

## READY SUBTYPES — which flow?

**Ready 4 Deal** (lead wants to BUY the course):
- Lead has: name + WhatsApp/phone + clear intent ("mau daftar", "gimana cara bayar", filled form)
- Set: `ready=true`, `ready_subtype='deal'`
- Manager flow: Send booking link, confirm DP amount, proceed to payment

**Ready 4 OpenHouse** (lead wants to ATTEND event):
- Lead has: name + contact + says "mau ke open house", "ada open house kapan", "mau lihat live demo"
- Set: `ready=true`, `ready_subtype='openhouse'`
- Manager flow: Send calendar details, confirm attendance, send reminder 24h before

## How to trigger each:

### Ready 4 Deal:
- Lead says "gimana cara daftar / saya mau ikut / cara bayar" WITH contact info
- Lead fills registration form (Nama/WhatsApp/Email/Program)
- After collecting contact for booking

### Ready 4 OpenHouse:
- Lead says "ada open house / mau lihat live / mau hadir 29 juni" WITH contact
- Lead RSVPs to event
- Alternative to buying now — soft-close when lead hesitates on price

## Response Templates:

**Ready 4 Deal:**
```
Siap Kak! Data daftar sudah kami catat. Tim akan hubungi untuk konfirmasi DP dan jadwal mulai 🚀
```

**Ready 4 OpenHouse:**
```
Wah seru! Kami catat nama + WA Kakak. Tim akan kirim detail open house 29 Juni ya. Sampai jumpa! 🎉
```
