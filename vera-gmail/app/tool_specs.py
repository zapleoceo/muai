from vera_shared.tools.spec import ToolParam, ToolSpec

TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="gmail_list_accounts",
        description="List Gmail accounts that Dima has connected to Vera. "
                    "Returns email addresses.",
        params=[],
    ),
    ToolSpec(
        name="gmail_list_threads",
        description=(
            "List recent Gmail threads in a connected account. Use this to "
            "find which threads to read. Pass a Gmail search query (e.g. "
            "'is:unread', 'from:boss@x.com', 'newer_than:7d label:inbox') or "
            "leave empty for the most recent inbox threads."
        ),
        params=[
            ToolParam("email", "string", "Connected Gmail address to query."),
            ToolParam("query", "string", "Gmail search query.",
                      required=False, default=""),
            ToolParam("max_results", "integer", "How many threads to return.",
                      required=False, default=20),
        ],
    ),
    ToolSpec(
        name="gmail_read_thread",
        description=(
            "Read all messages of a single Gmail thread, including text bodies, "
            "From/To/Subject headers, labels. When ocr_images=true (default) any "
            "embedded image attachments (screenshots of tables, scanned docs, "
            "photos with text) are OCR'd via Gemini and appended to each "
            "message's text — so you see tables that Gmail displays as inline "
            "images. has_ocr flag tells you which messages had image content."
        ),
        params=[
            ToolParam("email", "string", "Connected Gmail address."),
            ToolParam("thread_id", "string", "Gmail thread id from list_threads."),
            ToolParam("ocr_images", "boolean", "Run OCR on image attachments.",
                      required=False, default=True),
        ],
    ),
    ToolSpec(
        name="gmail_send_reply",
        description=(
            "Send a reply in an existing Gmail thread. Requires explicit user "
            "intent — never call this without the user choosing to reply."
        ),
        params=[
            ToolParam("email", "string", "Connected Gmail address (sender)."),
            ToolParam("thread_id", "string", "Thread to reply within."),
            ToolParam("to", "string", "Recipient email address."),
            ToolParam("subject", "string", "Subject line (Re: will be prefixed if missing)."),
            ToolParam("body", "string", "Reply body, plain text."),
            ToolParam("in_reply_to", "string", "Message-Id of the message being replied to.",
                      required=False, default=""),
        ],
    ),
    ToolSpec(
        name="gmail_modify_thread",
        description=(
            "Apply a label/state change to a Gmail thread. Actions: "
            "archive | trash | mark_read | mark_unread | star | unstar."
        ),
        params=[
            ToolParam("email", "string", "Connected Gmail address."),
            ToolParam("thread_id", "string", "Target thread id."),
            ToolParam("action", "string", "One of archive/trash/mark_read/mark_unread/star/unstar."),
        ],
    ),
    ToolSpec(
        name="gmail_modify_threads",
        description=(
            "BATCH version of gmail_modify_thread — apply the SAME action to "
            "many threads in one call. Use this whenever you would otherwise "
            "loop modify_thread N times (e.g. 'mark all Bybit unread as read'). "
            "Pass an array of thread_ids."
        ),
        params=[
            ToolParam("email", "string", "Connected Gmail address."),
            ToolParam("thread_ids", "array", "Array of thread ids to modify."),
            ToolParam("action", "string", "One of archive/trash/mark_read/mark_unread/star/unstar."),
        ],
    ),
    ToolSpec(
        name="gmail_apply_label",
        description=(
            "Add a label to one or more threads. Creates the label if it "
            "doesn't exist. Pass also_mark_read=true to mark threads as read "
            "in the same call. Use this for sorting threads into folders."
        ),
        params=[
            ToolParam("email", "string", "Connected Gmail address."),
            ToolParam("thread_ids", "array", "Array of thread ids."),
            ToolParam("label_name", "string", "Label name (e.g. 'Bybit'). Created if missing."),
            ToolParam("also_mark_read", "boolean", "Also remove UNREAD label.",
                      required=False, default=False),
        ],
    ),
]
