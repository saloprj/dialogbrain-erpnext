frappe.query_reports["First Response Time"] = {
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
				{ value: "Manager", label: __("Manager", null, "DialogBrain") },
				{ value: "Channel", label: __("Channel") },
				{ value: "Day", label: __("Day", null, "DialogBrain") },
			],
			default: "Manager",
		},
		{ fieldname: "channel", label: __("Channel"), fieldtype: "Link", options: "DB Channel" },
		{ fieldname: "assigned_to", label: __("Assigned to"), fieldtype: "Link", options: "User" },
	],
};
