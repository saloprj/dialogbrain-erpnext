// Customer / Lead form: "Chats" section with the conversations linked to this party.
// Loaded through doctype_js for both doctypes; the rendering lives in dialogbrain_desk.bundle.js.
["Customer", "Lead"].forEach((doctype) => {
	if (frappe.dialogbrain && frappe.dialogbrain._partyChatsBound && frappe.dialogbrain._partyChatsBound[doctype]) {
		return;
	}
	frappe.provide("frappe.dialogbrain._partyChatsBound");
	frappe.dialogbrain._partyChatsBound[doctype] = true;
	frappe.ui.form.on(doctype, {
		refresh(frm) {
			if (frappe.dialogbrain.renderPartyChats) frappe.dialogbrain.renderPartyChats(frm);
		},
	});
});
