from vera_shared.tools.spec import ToolParam, ToolSpec

TOOLS: list[ToolSpec] = [
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
            "from search_dialogs) OR peer (string name). Returns a list of "
            "messages with date, text, from, out flag."
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
]
