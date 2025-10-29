# -*- coding: utf-8 -*-
from __future__ import unicode_literals
import frappe
import json
from frappe.utils import flt, floor
from erpnext.accounts.doctype.sales_invoice.sales_invoice import SalesInvoice


class TallyTaxSalesInvoice(SalesInvoice):
	"""
	Custom Sales Invoice controller that applies Tally-compatible GST calculation
	AFTER ERPNext's standard tax calculation completes.
	
	This ensures your custom tax rounding and totals have the final say.
	"""

	def validate(self):
		"""
		Override validate to apply Tally tax logic after standard calculation.
		
		CRITICAL: We call super().validate() FIRST to let ERPNext do its
		standard calculation, then we override with Tally logic.
		This ensures our values persist and are not overwritten.
		"""
		# Run ERPNext's standard validation first (includes calculate_taxes_and_totals)
		super(TallyTaxSalesInvoice, self).validate()

		# Now apply Tally override - this runs LAST so values persist
		if not self.get("is_return") and self.docstatus == 0:
			self.apply_tally_tax_calculation()

	def apply_tally_tax_calculation(self):
		"""
		Apply Tally-compatible GST calculation with custom rounding.
		
		This method:
		1. Calculates item-wise taxes with Tally's round_half rounding
		2. Updates tax rows with correct amounts and item_wise_tax_detail
		3. Sets cumulative totals for each tax row
		4. Updates document totals with proper rounding adjustment
		"""
		
		# Step 1: Calculate item-wise taxes with Tally rounding
		total_cgst = 0.0
		total_sgst = 0.0
		total_igst = 0.0
		
		# Build item_wise_tax_detail dictionaries for each tax type
		# Format: {item_code: [rate, amount]}
		cgst_item_wise = {}
		sgst_item_wise = {}
		igst_item_wise = {}

		for item in self.items:
			amount = flt(item.get('amount', 0) or 0)
			cgst_rate = flt(item.get('cgst_rate', 0) or 0)
			sgst_rate = flt(item.get('sgst_rate', 0) or 0)
			igst_rate = flt(item.get('igst_rate', 0) or 0)

			# Tally rounding: round_half(amount * rate / 100, 2)
			# This rounds 0.5 up, which matches Tally's behavior
			cgst_amount = self.round_half(amount * cgst_rate / 100, 2)
			sgst_amount = self.round_half(amount * sgst_rate / 100, 2)
			igst_amount = self.round_half(amount * igst_rate / 100, 2)

			# Store in item fields for reference (optional but useful)
			item.cgst_amount = cgst_amount
			item.sgst_amount = sgst_amount
			item.igst_amount = igst_amount

			# Build item_wise_tax_detail dictionaries
			# This is used by ERPNext for tax breakup in print formats
			if cgst_rate > 0:
				cgst_item_wise[item.item_code] = [cgst_rate, cgst_amount]
			if sgst_rate > 0:
				sgst_item_wise[item.item_code] = [sgst_rate, sgst_amount]
			if igst_rate > 0:
				igst_item_wise[item.item_code] = [igst_rate, igst_amount]

			# Accumulate totals
			total_cgst += cgst_amount
			total_sgst += sgst_amount
			total_igst += igst_amount

		# Step 2: Calculate document-level totals
		total_tax_amount = total_cgst + total_sgst + total_igst
		doc_net_total = flt(self.net_total or 0)
		doc_base_total = flt(self.base_total or 0)

		grand_total = doc_net_total + total_tax_amount
		base_grand_total = doc_base_total + total_tax_amount

		# Calculate rounding adjustment using Tally logic (0.5 rounds up)
		decimal_part = base_grand_total - floor(base_grand_total)
		if decimal_part >= 0.5:
			base_rounded_total = floor(base_grand_total) + 1
		else:
			base_rounded_total = floor(base_grand_total)

		base_rounding_adjustment = base_rounded_total - base_grand_total

		# Step 3: Update tax rows with calculated amounts and item_wise_tax_detail
		for tax in self.taxes:
			# Identify tax type by checking gst_tax_type field or account_head
			gst_tax_type = (tax.get('gst_tax_type', "") or tax.get("account_head", "")).lower()

			if "cgst" in gst_tax_type:
				# Update all CGST-related fields
				tax.tax_amount = total_cgst
				tax.base_tax_amount = total_cgst
				tax.tax_amount_after_discount_amount = total_cgst
				tax.base_tax_amount_after_discount_amount = total_cgst
				# Set item_wise_tax_detail as JSON string
				tax.item_wise_tax_detail = json.dumps(cgst_item_wise)

			elif "sgst" in gst_tax_type or "utgst" in gst_tax_type:
				# Update all SGST/UTGST-related fields
				tax.tax_amount = total_sgst
				tax.base_tax_amount = total_sgst
				tax.tax_amount_after_discount_amount = total_sgst
				tax.base_tax_amount_after_discount_amount = total_sgst
				# Set item_wise_tax_detail as JSON string
				tax.item_wise_tax_detail = json.dumps(sgst_item_wise)

			elif "igst" in gst_tax_type:
				# Update all IGST-related fields
				tax.tax_amount = total_igst
				tax.base_tax_amount = total_igst
				tax.tax_amount_after_discount_amount = total_igst
				tax.base_tax_amount_after_discount_amount = total_igst
				# Set item_wise_tax_detail as JSON string
				tax.item_wise_tax_detail = json.dumps(igst_item_wise)

		# Step 4: Set cumulative running totals for tax rows
		# This is CRITICAL for CGST/SGST cases where there are 2 tax rows
		
		if len(self.taxes) == 1:
			# Interstate (IGST only) - single tax row
			tax = self.taxes
			tax.total = doc_base_total + total_igst
			tax.base_total = tax.total

		elif len(self.taxes) == 2:
			# Intrastate (CGST + SGST) - two tax rows with cumulative totals
			tax1 = self.taxes  # Usually CGST
			tax2 = self.taxes  # Usually SGST

			# First tax row: base_total + first tax
			tax1.total = doc_base_total + total_cgst
			tax1.base_total = tax1.total

			# Second tax row: base_total + first tax + second tax (cumulative)
			tax2.total = doc_base_total + total_cgst + total_sgst
			tax2.base_total = tax2.total

		# Step 5: Update document totals
		self.total_taxes_and_charges = total_tax_amount
		self.base_total_taxes_and_charges = total_tax_amount
		self.grand_total = grand_total
		self.base_grand_total = base_grand_total
		self.rounding_adjustment = base_rounding_adjustment
		self.rounded_total = self.round_half_up(grand_total)
		self.base_rounded_total = base_rounded_total
		self.outstanding_amount = base_rounded_total

	@staticmethod
	def round_half(n, decimals=2):
		"""
		Tally-compatible rounding: round to nearest, 0.5 rounds up.
		
		This matches Tally's rounding behavior exactly.
		
		Examples:
			round_half(10.125, 2) = 10.13  (0.005 rounds up)
			round_half(10.124, 2) = 10.12  (0.004 rounds down)
			round_half(10.5, 0) = 11       (0.5 rounds up)
		
		Args:
			n: Number to round
			decimals: Number of decimal places (default 2)
		
		Returns:
			Rounded float value
		"""
		multiplier = 10 ** decimals
		return float(int(n * multiplier + 0.5)) / multiplier

	@staticmethod
	def round_half_up(n):
		"""
		Round to nearest integer, 0.5 rounds up.
		
		Used for final grand total rounding.
		
		Examples:
			round_half_up(10.5) = 11
			round_half_up(10.4) = 10
			round_half_up(10.6) = 11
		
		Args:
			n: Number to round
		
		Returns:
			Rounded integer
		"""
		decimal_part = n - int(n)
		if decimal_part >= 0.5:
			return int(n) + 1
		else:
			return int(n)
