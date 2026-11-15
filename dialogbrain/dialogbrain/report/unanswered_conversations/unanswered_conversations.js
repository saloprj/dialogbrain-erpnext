frappe.query_reports["Unanswered Conversations"] = {
	filters: [
		{ fieldname: "channel", label: __("Channel"), fieldtype: "Link", options: "DB Channel" },
		{ fieldname: "assigned_to", label: __("Assigned to"), fieldtype: "Link", options: "User" },
		{
			fieldname: "min_waiting_minutes",
			label: __("Min waiting (min)"),
			fieldtype: "Int",
			default: 0,
		},
	],
	// The title and the preview are what the customer's channel sent: plain text that the
	// Data formatter would inject as HTML. Escape them here (exports keep the raw text).
	formatter(value, row, column, data, default_formatter) {
		if (["title", "last_message_preview"].includes(column.fieldname)) {
			return frappe.utils.escape_html(value == null ? "" : String(value));
		}
		return default_formatter(value, row, column, data);
	},
};
