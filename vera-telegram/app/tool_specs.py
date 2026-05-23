from vera_shared.tools.spec import ToolParam, ToolSpec

TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="telegram_list_recent_dialogs",
        description=(
            "List user's most recently active Telegram dialogs, sorted by last "
            "message date (newest first). Use this when the user asks about "
            "'active chats', 'recent chats', 'where I talked lately', without "
            "naming a specific peer. Returns id, name, type, unread_count, "
            "last_message_date for each."
        ),
        params=[
            ToolParam("limit", "integer", "How many dialogs to return.",
                      required=False, default=15),
            ToolParam("exclude_channels", "boolean",
                      "Skip broadcast channels (keep only people, groups, supergroups).",
                      required=False, default=False),
            ToolParam("only_unread", "boolean",
                      "Return only dialogs with unread messages.",
                      required=False, default=False),
        ],
    ),
    ToolSpec(
        name="telegram_search_dialogs",
        description=(
            "Find Telegram chats/channels/users whose name contains the query. "
            "Returns a list of candidates with id, name, type (user/bot/group/"
            "supergroup/channel) and username. Use this FIRST when the user "
            "mentions someone by partial name and you need to know the chat_id "
            "before reading or sending. Searches both Cyrillic and Latin "
            "transliterations automatically."
        ),
        params=[
            ToolParam("query", "string", "Search string (case-insensitive substring on dialog names)."),
            ToolParam("limit", "integer", "Max results.", required=False, default=15),
        ],
    ),
    ToolSpec(
        name="telegram_read_messages",
        description=(
            "Read recent messages from a Telegram chat. Pass chat_id (preferred, "
            "from search_dialogs) OR peer (string name). Returns each message "
            "with date, text, from, out, has_image, has_ocr. When a message "
            "contains a photo or image-document and ocr_images=true (default), "
            "the image is downloaded and OCR'd via Gemini — recognized text is "
            "appended to message.text as '[OCR]:\\n…'. So screenshots of "
            "balances, tables, receipts become readable. Bounded to 6 OCR "
            "calls per request."
        ),
        params=[
            ToolParam("chat_id", "integer", "Telegram entity id from search_dialogs. Preferred over peer.",
                      required=False, default=0),
            ToolParam("peer", "string", "Chat name as fallback when chat_id unknown.",
                      required=False, default=""),
            ToolParam("days", "integer", "How many days back to read.",
                      required=False, default=1),
            ToolParam("limit", "integer", "Max messages to fetch.",
                      required=False, default=50),
            ToolParam("ocr_images", "boolean", "Run OCR on image attachments.",
                      required=False, default=True),
        ],
    ),
    ToolSpec(
        name="telegram_send_message",
        description="Send a Telegram message. Pass chat_id (preferred) or peer.",
        params=[
            ToolParam("chat_id", "integer", "Target chat id.", required=False, default=0),
            ToolParam("peer", "string", "Target chat name.", required=False, default=""),
            ToolParam("text", "string", "Message body."),
        ],
    ),
    ToolSpec(
        name="telegram_get_dialog_info",
        description="Get info about a Telegram chat: name, type, participants count.",
        params=[
            ToolParam("chat_id", "integer", "Target chat id.", required=False, default=0),
            ToolParam("peer", "string", "Target chat name.", required=False, default=""),
        ],
    ),
    ToolSpec(
        name="telegram_delete_messages",
        description=(
            "Delete specific Telegram messages by id. Own messages always "
            "deletable; others require admin with delete_messages right. "
            "revoke=True deletes from both sides; False — only from your view."
        ),
        params=[
            ToolParam("chat_id", "integer", "Target chat id.", required=False, default=0),
            ToolParam("peer", "string", "Target chat name.", required=False, default=""),
            ToolParam("message_ids", "array",
                      "List of message ids to delete (integers)."),
            ToolParam("revoke", "boolean",
                      "Delete for both sides (default true).",
                      required=False, default=True),
        ],
    ),
    ToolSpec(
        name="telegram_clear_history",
        description=(
            "Wipe entire chat history with a peer. Use carefully — "
            "destructive. just_clear=True hides from your side only. "
            "revoke=True attempts both-side deletion (works in personal "
            "chats; in groups needs admin)."
        ),
        params=[
            ToolParam("chat_id", "integer", "Target chat id.", required=False, default=0),
            ToolParam("peer", "string", "Target chat name.", required=False, default=""),
            ToolParam("just_clear", "boolean",
                      "Hide from your side only (safe).",
                      required=False, default=False),
            ToolParam("revoke", "boolean",
                      "Delete for both sides (irreversible).",
                      required=False, default=False),
            ToolParam("max_id", "integer",
                      "Delete up to this msg id (0 = all).",
                      required=False, default=0),
        ],
    ),
]
