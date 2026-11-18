// Keep the first column of every query report in view while the table scrolls sideways.
// Wide reports (Profit and Loss by month, Trial Balance, ...) push the account names off the
// screen; the datatable already has a "Freeze" column action, so this turns it on for the
// first real column (index 0 is the row number) right after each render. The person can still
// switch it off from the column menu ("Unfreeze") until the next refresh.

const FROZEN = 1; // datatable column index: 0 = row number, 1 = the first report column
const MIN_COLUMNS = 4; // narrow reports fit on screen; leave them alone

function freezeFirstColumn(datatable) {
	try {
		const columns = datatable && datatable.datamanager && datatable.datamanager.getColumns();
		if (!columns || columns.length <= MIN_COLUMNS) return;
		const col = columns[FROZEN];
		if (!col || col.sticky) return;
		datatable.setColumnSticky(FROZEN, true);
	} catch (e) {
		// never break a report over a convenience
		console.warn("dialogbrain: could not freeze the first report column", e);
	}
}

function patchQueryReport() {
	const QueryReport = frappe.views && frappe.views.QueryReport;
	if (!QueryReport || QueryReport.prototype.__dialogbrain_freeze) return !!QueryReport;
	const original = QueryReport.prototype.render_datatable;
	QueryReport.prototype.render_datatable = function (...args) {
		const result = original.apply(this, args);
		freezeFirstColumn(this.datatable);
		return result;
	};
	QueryReport.prototype.__dialogbrain_freeze = true;
	// a report already on screen when the patch lands
	if (frappe.query_report && frappe.query_report.datatable) freezeFirstColumn(frappe.query_report.datatable);
	return true;
}

// The report code is a separate bundle that loads the first time a report is opened, so try now
// and again on each route change until it is there.
$(document).on("startup", patchQueryReport);
frappe.router && frappe.router.on && frappe.router.on("change", () => {
	if (!patchQueryReport()) setTimeout(patchQueryReport, 1500);
});
