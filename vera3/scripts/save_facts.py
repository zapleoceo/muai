"""Save Dmytro's career facts from CVs (CTO HTML + IT Director PDF)."""
import asyncio, hashlib, os, httpx

FACTS = [
    # ─── COLLECTIONS — главное ─────────────────────────────────────────
    (["bio", "privatbank", "credit-collection"],
     "Дмитрий Запорожец — Deputy Head, Credit Collection в ПриватБанке 2012–2017 (5 лет). Это его профильная collections-роль. Возглавлял разработку и развёртывание автоматизационных систем PrivatCollect и MobileCollect — обе дали x2–4 productivity gain (рост производительности collection-операций в 2–4 раза). Это его ключевая релевантная экспертиза для вакансий уровня PM/Director в legal collections automation, debt recovery, fintech collections."),
    (["bio", "privatbank", "scale"],
     "Дмитрий Запорожец в ПриватБанке управлял внутренними IT-командами 30+ человек, проекты обслуживали 20M+ клиентов банка. Курировал annual infrastructure CAPEX $3M+/год — полный procurement-цикл и quality audits. Реализованные process improvements снизили операционные расходы на 40%. Это масштаб большой структурной компании с регулируемым домейном."),
    (["bio", "privatbank", "stack"],
     "Дмитрий Запорожец в ПриватБанке Credit Collection: построил Power BI дашборды для executive reporting, дизайнил SQL-based analytics pipelines (sprint planning, QA, release management как Product Owner). Был драйвером Agile/Scrum adoption как часть broader digital transformation. Совмещал роли IT Delivery / Project Manager / Product Owner в одном профиле."),
    (["bio", "privatbank", "career-arc"],
     "Дмитрий Запорожец полный карьерный путь в ПриватБанке: 2005–2012 (7 лет) — Various IT & Operations roles, progressed from logistics/sales support → IT coordination → university partnerships. 2012–2017 (5 лет) — Deputy Head Credit Collection / IT Delivery / Project Manager / Product Owner. Итого 12 лет в крупнейшем банке Украины с фокусом на core banking, collections automation, digital transformation."),

    # ─── ZapleoSoft ────────────────────────────────────────────────────
    (["bio", "zapleosoft", "founding"],
     "Дмитрий Запорожец — Founder & CEO ZapleoSoft (zapleo.com) 2017–наст.вр. Веб/мобильное dev-агентство для международных клиентов. Шкалировал 3 → 25+ engineers. Доставил 500+ проектов. Сейчас в support/maintenance mode (новые проекты на паузе из-за ситуации с войной в Украине)."),
    (["bio", "zapleosoft", "clients"],
     "Дмитрий Запорожец / ZapleoSoft — notable clients: OLX (integration), Instytutum, Levaromat. Работал с 100k+/mo traffic платформами. Revenue вырос beyond $40k. Снизил OPEX на 40% через automation и vendor renegotiation."),
    (["bio", "zapleosoft", "enterprise-projects"],
     "Дмитрий Запорожец / ZapleoSoft — крупные enterprise проекты: Workday ERP rollout для 1000+ user company — от дизайна до post-launch support. Hybrid cloud migration: 200+ VMs из on-prem в Azure/MS 365 с 99.99% uptime SLA. Successful passing of ISO 27001 / ISO 9001 / SOC2 audits в двух организациях."),

    # ─── Pasijou Sri Lanka ─────────────────────────────────────────────
    (["bio", "pasijou"],
     "Дмитрий Запорожец — Co-owner Pasijou Coworking & Restaurant в Weligama, Sri Lanka (Feb 2023 – Mar 2026). Проект закрылся в марте 2026 (end of lease). Полная сетевая инфраструктура: MikroTik routing, VPN, mesh Wi-Fi, load balancing, power backup. Запустил digital operations (ordering, analytics, guest communications). Breakeven за 8 месяцев, поддерживали рейтинг 4.9/5 throughout. Это его первый Юго-Восточный Азиатский предпринимательский проект."),

    # ─── Veranda (уточнение) ───────────────────────────────────────────
    (["bio", "veranda", "title-clarify"],
     "Дмитрий Запорожец — точное название должности в Veranda: Co-owner & COO / Product Manager (Aug 2025 – Present). Архитектировал full digital stack: online ordering, POS integration, SMM automation, analytics dashboards. Owned product roadmap для digital operations layer. A/B тестирование офферов и коммуникаций. Сетевая инфра: Wi-Fi, CCTV, NAS, backup. Data-based решения по pricing, меню, акциям."),

    # ─── Education & Certifications ────────────────────────────────────
    (["bio", "education"],
     "Дмитрий Запорожец — Master's Degree in Automation and Computer-Integrated Technologies (= Instruments & Systems of Non-Destructive Testing на CTO CV, та же специальность), Oles Honchar Dnipro National University, 2002–2007. MBA Candidate 2025 (online track)."),
    (["bio", "certifications"],
     "Дмитрий Запорожец — сертификации: Microsoft Certified Azure Administrator, ITIL v4 Foundation, Scrum Master PSM I. ВСЕ ТРИ актуальны и подтверждены."),

    # ─── Скиллы / overarching ──────────────────────────────────────────
    (["bio", "experience-total"],
     "Дмитрий Запорожец — 18+ лет общего опыта в digital transformation, enterprise IT management, complex team leadership. Эксперт в hybrid cloud, ERP implementations, large-scale infrastructure, business process automation. Подтверждённый трек в financial services (PrivatBank) и IT consulting (ZapleoSoft). Лидировал команды 20–50+ в EMEA и South Asia."),
    (["bio", "languages"],
     "Дмитрий Запорожец — языки: Ukrainian C2 (native), Russian C2 (native), English B2 (working / presentations — честная самооценка из CV, на практике ежедневно общается с международными HQ и клиентами)."),

    # ─── Compliance / regulated environments ────────────────────────────
    (["bio", "compliance"],
     "Дмитрий Запорожец — опыт работы в regulated environments: ISO 27001, ISO 9001, SOC2 (passed audits в двух организациях), ITSM, SDLC, CI/CD. Это критично для legal collections / fintech вакансий где нужно понимать как продукт переживёт регуляторный audit. Опыт business continuity, risk management, audit."),

    # ─── Repositioning vs original brain ───────────────────────────────
    (["bio", "correction-collection-yes"],
     "ВАЖНОЕ ОБНОВЛЕНИЕ ПАМЯТИ (20.06.2026): ранее в мозге была неточность — мы думали что у Дмитрия в ПриватБанке не было collections-опыта. Это НЕ ТАК. У него 5 лет Deputy Head Credit Collection (2012–2017) + он построил PrivatCollect и MobileCollect (x2–4 productivity gain). Это его ключевая релевантность для legal collections automation вакансий. Прежний факт о PrivatBank mobile app (20 лет назад, во время до распространения mobile apps) тоже верен — это был самый ранний продуктовый проект в банке, до collections-роли."),
]

async def main():
    secret = os.environ["INTERNAL_SECRET"]
    sent = 0
    async with httpx.AsyncClient(timeout=15) as c:
        for tags, text in FACTS:
            sig = f"{','.join(tags)}|{text[:120]}"
            eid = "fact:" + hashlib.sha1(sig.encode()).hexdigest()[:16]
            r = await c.post("http://gateway:8000/event/vera_memory",
                json={"source":"vera_memory","source_event_id":eid,"account":"vera","category":"fact",
                      "content_text":text[:8000],"occurred_at":"2026-06-20T02:00:00",
                      "metadata":{"tags":tags,"confidence":0.98}},
                headers={"X-Internal-Secret":secret})
            ok = r.status_code < 400
            sent += ok
            print(f"{'✓' if ok else '✗'} {'/'.join(tags):40} {text[:55]}…")
            await asyncio.sleep(0.05)
    print(f"\nDone: {sent}/{len(FACTS)} saved.")

asyncio.run(main())
