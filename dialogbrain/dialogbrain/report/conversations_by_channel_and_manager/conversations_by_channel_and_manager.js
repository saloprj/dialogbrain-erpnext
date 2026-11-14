frappe.query_reports["Conversations by Channel and Manager"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date", null, "DialogBrain"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -30),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date", null, "DialogBrain"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "group_by",
			label: __("Group By", null, "DialogBrain"),
			fieldtype: "Select",
			options: [
				{ value: "Channel", label: __("Channel") },
				{ value: "Manager", label: __("Manager", null, "DialogBrain") },
			],
			default: "Channel",
		},
	],
};
