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
};
