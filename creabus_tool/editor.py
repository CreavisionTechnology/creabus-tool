# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Build and edit device profiles without leaving the window.

A profile is a plain YAML file and editing it in a text editor is still the
quickest way to make a big change. This is for the other case: working out a
register map against real hardware, where you want to add a register, try it,
fix the type, and try again without alt-tabbing.

Nothing is written until Save, and nothing is saved that would not load: the
same validation the loader applies runs first, so a profile that saves is a
profile that opens.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import theme
from .profile import (
    BIT_TABLES,
    CONVENTIONAL_BASE,
    MODES,
    NUMERIC_TYPES,
    TABLES,
    DeviceProfile,
    ProfileError,
    parse_profile,
    save_profile,
)

TYPES = (*sorted(NUMERIC_TYPES), "string")

# field name -> (label, help shown under the form)
_HELP = {
    "name": "identifier used in expressions: letters, digits, underscore",
    "address": "protocol address, zero based. 12 or 0x000C both work",
    "table": "input and discrete are read only; holding and coil can be written",
    "type": "how the value is laid out across the registers",
    "mode": "how the simulated value moves. Ignored when polling a real device",
    "min": "lower limit, required by walk and random",
    "max": "upper limit, required by walk and random",
    "step": "biggest jump per update in walk mode",
    "start": "value it begins at",
    "scale": "raw * scale = engineering value, for devices storing tenths",
    "expression": "computed from other registers, e.g. voltage * current",
    "rate": "units per second, for accumulator mode",
    "interval": "seconds between changes to this register",
    "decimals": "digits shown after the point",
    "length": "characters, for a string register",
}


class ProfileEditor(tk.Toplevel):
    """A modeless editor over a copy of a profile."""

    def __init__(self, parent, profile: DeviceProfile, devices_dir: str,
                 on_saved=None):
        super().__init__(parent)
        self.title(f"Edit profile - {profile.title}")
        self.devices_dir = devices_dir
        self._on_saved = on_saved
        self.transient(parent)
        self.minsize(1060, 620)
        self.geometry("1180x680")

        # Work on a detached copy: closing without saving must leave the
        # running device exactly as it was.
        self._data = profile.to_dict()
        self._path = profile.source_path
        self._dirty = False

        self._build()
        self._reload_list()
        if self.rows:
            self.tree.selection_set(self.rows[0])
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill="both", expand=True)

        device_box = ttk.LabelFrame(outer, text="Device", padding=8)
        device_box.pack(fill="x")
        self.device_vars: dict[str, tk.Variable] = {}
        row = ttk.Frame(device_box)
        row.pack(fill="x")
        for key, label, width in (("name", "Name:", 26), ("model", "Model:", 16),
                                  ("vendor", "Vendor:", 14)):
            ttk.Label(row, text=label).pack(side="left", padx=(0, 4))
            var = tk.StringVar(value=str(self._data["device"].get(key, "")))
            var.trace_add("write", lambda *_a: self._touch())
            ttk.Entry(row, textvariable=var, width=width).pack(side="left", padx=(0, 12))
            self.device_vars[key] = var

        row2 = ttk.Frame(device_box)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="Unit id:").pack(side="left", padx=(0, 4))
        self.device_vars["unit_id"] = tk.StringVar(
            value=str(self._data["device"].get("unit_id", 1)))
        ttk.Entry(row2, textvariable=self.device_vars["unit_id"], width=6).pack(side="left")
        ttk.Label(row2, text="Default interval:").pack(side="left", padx=(12, 4))
        self.device_vars["default_interval"] = tk.StringVar(
            value=str(self._data["device"].get("default_interval", 30)))
        ttk.Entry(row2, textvariable=self.device_vars["default_interval"],
                  width=6).pack(side="left")
        ttk.Label(row2, text="s").pack(side="left", padx=(2, 0))
        for key, label, values in (("word_order", "Word order:", ("big", "little")),
                                   ("byte_order", "Byte order:", ("big", "little")),
                                   ("gap_policy", "Gaps:", ("zero", "exception"))):
            ttk.Label(row2, text=label).pack(side="left", padx=(12, 4))
            var = tk.StringVar(value=str(self._data["device"].get(key, values[0])))
            self.device_vars[key] = var
            ttk.Combobox(row2, textvariable=var, values=values, state="readonly",
                         width=9).pack(side="left")
        for var in self.device_vars.values():
            var.trace_add("write", lambda *_a: self._touch())

        middle = ttk.Frame(outer)
        middle.pack(fill="both", expand=True, pady=(8, 0))

        # The field panel is packed before the list: pack gives space in order,
        # so an expanding list packed first would take the lot and leave the
        # fields with nothing to draw in.
        right = ttk.LabelFrame(middle, text="Selected register", padding=8)
        right.pack(side="right", fill="y", padx=(8, 0))

        # --- the register list --------------------------------------------
        left = ttk.LabelFrame(middle, text="Registers", padding=6)
        left.pack(side="left", fill="both", expand=True)
        columns = ("name", "table", "address", "conv", "type", "mode")
        self.tree = ttk.Treeview(left, columns=columns, show="headings",
                                 selectmode="browse", height=16)
        for column, title, width in (("name", "Name", 132), ("table", "Table", 58),
                                     ("address", "Address", 74), ("conv", "4xxxx", 60),
                                     ("type", "Type", 62), ("mode", "Mode", 92)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, stretch=column == "name")
        # Again, packed before the tree so the expanding tree cannot swallow the
        # space these need.
        buttons = ttk.Frame(left)
        buttons.pack(side="bottom", fill="x", pady=(6, 0))
        for text, command in (("Add", self.add_register),
                              ("Duplicate", self.duplicate_register),
                              ("Delete", self.delete_register),
                              ("Up", lambda: self.move_register(-1)),
                              ("Down", lambda: self.move_register(1))):
            ttk.Button(buttons, text=text, width=9, command=command).pack(side="left", padx=1)

        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_selected())

        # --- the fields of one register ------------------------------------
        self.field_vars: dict[str, tk.Variable] = {}
        self._widgets: dict[str, tk.Widget] = {}
        # Left column: what the register is and where it lives.
        # Right column: how its value behaves and how it is shown.
        fields = (
            ("name", "Name", "entry", None),
            ("label", "Label", "entry", None),
            ("table", "Table", "combo", TABLES),
            ("address", "Address", "entry", None),
            ("type", "Type", "combo", TYPES),
            ("unit", "Unit", "entry", None),
            ("scale", "Scale", "entry", None),
            ("decimals", "Decimals", "entry", None),
            ("length", "Length (string)", "entry", None),

            ("mode", "Mode", "combo", MODES),
            ("min", "Min", "entry", None),
            ("max", "Max", "entry", None),
            ("step", "Step", "entry", None),
            ("start", "Start", "entry", None),
            ("interval", "Interval (s)", "entry", None),
            ("expression", "Expression", "entry", None),
            ("rate", "Rate (per second)", "entry", None),
            ("comment", "Comment", "entry", None),
        )
        for index, (key, label, kind, values) in enumerate(fields):
            r, c = index % 9, (index // 9) * 2
            ttk.Label(right, text=f"{label}:").grid(row=r, column=c, sticky="e", padx=(0, 4),
                                                    pady=2)
            var = tk.StringVar()
            self.field_vars[key] = var
            if kind == "combo":
                widget = ttk.Combobox(right, textvariable=var, values=values,
                                      state="readonly", width=18)
                widget.bind("<<ComboboxSelected>>", lambda _e, k=key: self._field_changed(k))
            else:
                widget = ttk.Entry(right, textvariable=var, width=20)
                widget.bind("<FocusOut>", lambda _e, k=key: self._field_changed(k))
                widget.bind("<Return>", lambda _e, k=key: self._field_changed(k))
            widget.grid(row=r, column=c + 1, sticky="w", pady=2)
            self._widgets[key] = widget
            widget.bind("<Enter>", lambda _e, k=key: self._hint(k))

        self.wrap_var = tk.BooleanVar()
        wrap = ttk.Checkbutton(right, text="wrap at max (accumulator)", variable=self.wrap_var,
                               command=lambda: self._field_changed("wrap"))
        wrap.grid(row=9, column=2, columnspan=2, sticky="w", pady=(6, 0))
        self._widgets["wrap"] = wrap

        self.hint = ttk.Label(right, text="", foreground=theme.palette_for(self).muted,
                              wraplength=430)
        self.hint.grid(row=10, column=0, columnspan=4, sticky="w", pady=(10, 0))

        # --- the buttons at the bottom --------------------------------------
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(8, 0))
        self.status = ttk.Label(bar, text="")
        self.status.pack(side="left")
        ttk.Button(bar, text="Close", command=self.close).pack(side="right")
        ttk.Button(bar, text="Save as...", command=self.save_as).pack(side="right", padx=6)
        ttk.Button(bar, text="Save", command=self.save).pack(side="right")
        ttk.Button(bar, text="Check", command=self.check).pack(side="right", padx=6)

    # ------------------------------------------------------------------ state
    def _touch(self) -> None:
        self._dirty = True
        self._set_status("unsaved changes")

    def _set_status(self, text: str) -> None:
        self.status.config(text=text)

    def _hint(self, key: str) -> None:
        self.hint.config(text=_HELP.get(key, ""))

    @property
    def registers(self) -> list[dict]:
        return self._data["registers"]

    def _reload_list(self, keep: int | None = None) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.rows: list[str] = []
        for index, reg in enumerate(self.registers):
            table = str(reg.get("table", "input"))
            try:
                address = int(str(reg.get("address", 0)), 0)
            except ValueError:
                address = 0
            conv = CONVENTIONAL_BASE.get(table, 0) + address
            iid = self.tree.insert("", "end", iid=str(index), values=(
                reg.get("name", ""), table, f"0x{address:04X}", conv,
                "bool" if table in BIT_TABLES else reg.get("type", "float32"),
                reg.get("mode", "random"),
            ))
            self.rows.append(iid)
        if keep is not None and self.rows:
            index = max(0, min(keep, len(self.rows) - 1))
            self.tree.selection_set(self.rows[index])
            self.tree.see(self.rows[index])

    def _selected_index(self) -> int | None:
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def _show_selected(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        reg = self.registers[index]
        self._loading = True
        for key, var in self.field_vars.items():
            value = reg.get(key, "")
            var.set("" if value is None else str(value))
        self.wrap_var.set(bool(reg.get("wrap", False)))
        self._loading = False
        # bit tables have no type or scale of their own
        is_bit = str(reg.get("table", "input")) in BIT_TABLES
        for key in ("type", "scale", "decimals", "length"):
            self._widgets[key].configure(state="disabled" if is_bit else
                                         ("readonly" if key == "type" else "normal"))

    def _field_changed(self, key: str) -> None:
        index = self._selected_index()
        if index is None or getattr(self, "_loading", False):
            return
        reg = self.registers[index]
        if key == "wrap":
            reg["wrap"] = self.wrap_var.get()
        else:
            text = self.field_vars[key].get().strip()
            if text == "":
                reg.pop(key, None)
            else:
                reg[key] = _coerce(key, text)
        self._touch()
        self._reload_list(keep=index)

    # ----------------------------------------------------------------- edits
    def add_register(self) -> None:
        existing = {r.get("name") for r in self.registers}
        number = len(self.registers) + 1
        while f"register_{number}" in existing:
            number += 1
        highest = 0
        for reg in self.registers:
            try:
                highest = max(highest, int(str(reg.get("address", 0)), 0))
            except ValueError:
                pass
        self.registers.append({
            "name": f"register_{number}", "table": "holding",
            "address": f"0x{highest + 2:04X}", "type": "float32",
            "mode": "fixed", "start": 0, "interval": 30,
        })
        self._touch()
        self._reload_list(keep=len(self.registers) - 1)

    def duplicate_register(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        copy = dict(self.registers[index])
        names = {r.get("name") for r in self.registers}
        base = str(copy.get("name", "register"))
        suffix = 2
        while f"{base}_{suffix}" in names:
            suffix += 1
        copy["name"] = f"{base}_{suffix}"
        try:                       # park it clear of the original
            copy["address"] = f"0x{int(str(copy.get('address', 0)), 0) + 2:04X}"
        except ValueError:
            pass
        self.registers.insert(index + 1, copy)
        self._touch()
        self._reload_list(keep=index + 1)

    def delete_register(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        name = self.registers[index].get("name", "")
        if not messagebox.askyesno("Delete register", f"Remove '{name}'?", parent=self):
            return
        self.registers.pop(index)
        self._touch()
        self._reload_list(keep=index)

    def move_register(self, delta: int) -> None:
        index = self._selected_index()
        if index is None:
            return
        target = index + delta
        if not 0 <= target < len(self.registers):
            return
        self.registers[index], self.registers[target] = \
            self.registers[target], self.registers[index]
        self._touch()
        self._reload_list(keep=target)

    # ------------------------------------------------------------------ save
    def _validated(self) -> DeviceProfile | None:
        """The profile as it would be loaded, or None after reporting why not."""
        device = self._data["device"]
        for key, caster in (("unit_id", int), ("default_interval", float)):
            text = str(self.device_vars[key].get()).strip()
            try:
                device[key] = caster(text)
            except ValueError:
                messagebox.showerror("Device", f"'{key}' must be a number, got {text!r}",
                                     parent=self)
                return None
        for key in ("name", "model", "vendor", "word_order", "byte_order", "gap_policy"):
            device[key] = str(self.device_vars[key].get()).strip()
        try:
            return parse_profile(self._data, self._path or "<editor>")
        except ProfileError as exc:
            messagebox.showerror("Profile is not valid yet", str(exc), parent=self)
            return None

    def check(self) -> None:
        profile = self._validated()
        if profile is not None:
            messagebox.showinfo(
                "Profile is valid",
                f"{profile.title}\n{len(profile.registers)} registers, "
                f"unit id {profile.unit_id}.", parent=self)
            self._set_status("valid")

    def save(self) -> None:
        if not self._path:
            self.save_as()
            return
        profile = self._validated()
        if profile is None:
            return
        try:
            save_profile(profile, self._path)
        except (ProfileError, OSError) as exc:
            messagebox.showerror("Could not save", str(exc), parent=self)
            return
        self._dirty = False
        self._set_status(f"saved to {os.path.basename(self._path)}")
        if self._on_saved:
            self._on_saved(self._path)

    def save_as(self) -> None:
        profile = self._validated()
        if profile is None:
            return
        suggested = os.path.basename(self._path) if self._path else "my-device.yaml"
        path = filedialog.asksaveasfilename(
            parent=self, title="Save device profile", initialdir=self.devices_dir,
            initialfile=suggested, defaultextension=".yaml",
            filetypes=[("YAML profile", "*.yaml"), ("JSON profile", "*.json")])
        if not path:
            return
        try:
            save_profile(profile, path)
        except (ProfileError, OSError) as exc:
            messagebox.showerror("Could not save", str(exc), parent=self)
            return
        self._path = path
        self._dirty = False
        self.title(f"Edit profile - {os.path.basename(path)}")
        self._set_status(f"saved to {os.path.basename(path)}")
        if self._on_saved:
            self._on_saved(path)

    def close(self) -> None:
        if self._dirty and not messagebox.askyesno(
                "Discard changes?", "This profile has unsaved changes. Close anyway?",
                parent=self):
            return
        self.destroy()


def _coerce(key: str, text: str):
    """Turn a form field back into the type the profile schema expects."""
    if key == "address":
        try:
            return f"0x{int(text, 0):04X}"
        except ValueError:
            return text                       # let the validator complain properly
    if key in ("min", "max", "step", "scale", "interval", "rate_value"):
        try:
            return float(text)
        except ValueError:
            return text
    if key in ("decimals", "length"):
        try:
            return int(text)
        except ValueError:
            return text
    if key == "start":
        try:
            return float(text)
        except ValueError:
            return text                       # a string register starts as text
    return text
