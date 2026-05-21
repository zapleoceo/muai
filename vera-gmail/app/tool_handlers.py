from app.api import (
    apply_label, list_accounts, list_threads, modify_thread, modify_threads,
    read_thread, send_reply,
)


HANDLERS = {
    "gmail_list_accounts":  lambda **_: list_accounts(),
    "gmail_list_threads":   lambda email, query="", max_results=20, **_:
        list_threads(str(email), str(query), int(max_results)),
    "gmail_read_thread":    lambda email, thread_id, ocr_images=True, **_:
        read_thread(str(email), str(thread_id), ocr_images=bool(ocr_images)),
    "gmail_send_reply":     lambda email, thread_id, to, subject, body, in_reply_to="", **_:
        send_reply(str(email), str(thread_id), str(to), str(subject), str(body),
                   in_reply_to=str(in_reply_to) or None),
    "gmail_modify_thread":  lambda email, thread_id, action, **_:
        modify_thread(str(email), str(thread_id), str(action)),
    "gmail_modify_threads": lambda email, thread_ids, action, **_:
        modify_threads(str(email), [str(t) for t in (thread_ids or [])], str(action)),
    "gmail_apply_label":    lambda email, thread_ids, label_name, also_mark_read=False, **_:
        apply_label(str(email), [str(t) for t in (thread_ids or [])],
                    str(label_name), bool(also_mark_read)),
}
