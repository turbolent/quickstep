#!/bin/sh
# Run after User Patch 4 and FramebufferWC have successful receipts.
# An alternate driver root lets us test without changing the installed system.
PATH=/bin:/usr/bin
export PATH

fail() { echo "setup-framebuffer-wc: $*" >&2; exit 1; }
# This shell has no test -e, and its ls succeeds even for missing paths.
exists() { [ -h "$1" ] || [ -f "$1" ] || [ -d "$1" ] || [ -r "$1" ]; }
regular() { [ -f "$1" ] && [ ! -h "$1" ]; }

[ "$#" -le 1 ] || fail "Usage: setup-framebuffer-wc.sh [/absolute/driver/root]"
root=${1:-/private/Drivers/i386}
case "$root" in /?*) ;; *) fail "Driver root must be absolute" ;; esac
system="$root/System.config/Instance0.table"
regular "$system" || fail "Expected a regular System.config/Instance0.table"

# Stage on the destination filesystem; never edit a live table in place.
stage="$root/System.config/.framebuffer-wc.$$"
umask 077
mkdir "$stage" || fail "Cannot create staging directory"
# The old shell loses exit N across an exit trap, so track success explicitly.
exit_status=1
trap 'trap 0; rm -f "$stage/VBE20DisplayDriver.original" "$stage/VBE20DisplayDriver.new" \
    "$stage/VBE20DisplayDriver.create" "$stage/FramebufferWC.original" \
    "$stage/FramebufferWC.new" "$stage/FramebufferWC.create" \
    "$stage/System.original" "$stage/System.new" "$stage/System.edited"; \
    rmdir "$stage"; exit "$exit_status"' 0
trap 'exit_status=1; exit 1' 1 2 3 15
for name in VBE20DisplayDriver FramebufferWC
do
    instance="$root/$name.config/Instance0.table"
    source="$instance"
    if exists "$instance"; then
        regular "$instance" || fail "Expected a regular file: $instance"
    else
        source="$root/$name.config/Default.table"
        regular "$source" || fail "Missing driver defaults: $source"
        : > "$stage/$name.create"
    fi
    cp -p "$source" "$stage/$name.original" &&
        cp -p "$source" "$stage/$name.new" &&
        tr -d '\015' < "$source" > "$stage/$name.new" || fail "Cannot stage $source"
done
cp -p "$system" "$stage/System.original" && cp -p "$system" "$stage/System.new" ||
    fail "Cannot stage System.config"

# OPENSTEP ships the original awk: no functions, -v, sub(), or ternary syntax.
# Tokenize quoted table entries so comments, duplicate keys, escaped quotes and
# multi-line entries cannot be mistaken for driver settings. Preserve other text.
awk '
{ data[table] = data[table] $0 "\n" }
END {
    boot[1] = 0; boot[2] = 0
    for (t = 1; t <= 3; t++) {
        text = data[t]; state = 0
        for (i = 1; i <= length(text); i++) {
            c = substr(text, i, 1); pair = substr(text, i, 2)
            if (c ~ /[ \t\r\n]/) continue
            if (pair == "/*") {
                end = index(substr(text, i + 2), "*/")
                if (!end) { print "Unterminated table comment"; exit 1 }
                i += end + 2; continue
            }
            if (pair == "//") {
                end = index(substr(text, i), "\n")
                if (!end) break
                i += end - 1; continue
            }
            if (c == "\"" && (state == 0 || state == 2)) {
                if (state == 0) start = i
                value = ""
                for (i++; i <= length(text); i++) {
                    c = substr(text, i, 1)
                    if (c == "\"") break
                    if (c == "\\") { i++; c = substr(text, i, 1) }
                    value = value c
                }
                if (i > length(text)) { print "Unterminated table string"; exit 1 }
                if (state == 0) { key = value; state = 1 }
                else state = 3
                continue
            }
            if (c == "=" && state == 1) { state = 2; continue }
            if (c != ";" || (state != 1 && state != 3)) {
                print "Invalid driver table syntax in table " t; exit 1
            }
            if (t < 3 && key == "Boot Driver") {
                if (state == 1) value = "Yes"
                while (substr(value, 1, 1) ~ /[ \t\r\n]/) value = substr(value, 2)
                while (substr(value, length(value), 1) ~ /[ \t\r\n]/)
                    value = substr(value, 1, length(value) - 1)
                boot[t] = 0
                if (value ~ /^([Yy][Ee][Ss]|[Tt][Rr][Uu][Ee]|1|[Bb][Oo][Oo][Tt] [Dd][Rr][Ii][Vv][Ee][Rr])$/) boot[t] = 1
                else if (value != "" && value !~ /^([Nn][Oo]|[Ff][Aa][Ll][Ss][Ee]|0)$/) {
                    print "Unknown Boot Driver setting: " value; exit 1
                }
            }
            if (t == 3 && (key == "Boot Drivers" || key == "Active Drivers")) {
                if (state == 1) { print key " requires a quoted value"; exit 1 }
                lists[key] = value
                entries++; first[entries] = start; last[entries] = i; keys[entries] = key
                final[key] = entries
            }
            state = 0
        }
        if (state != 0) { print "Incomplete driver table entry"; exit 1 }
    }
    if (boot[2] == 1 && boot[1] == 0) {
        print "FramebufferWC cannot be a boot driver when VBE is an active driver"; exit 1
    }
    for (t = 1; t <= 2; t++) {
        key = "Active Drivers"; target = 0
        if (t == 1) { key = "Boot Drivers"; target = 1 }
        count = split(lists[key], names, " "); kept = 0; position = -1
        for (i = 1; i <= count; i++) {
            name = names[i]
            if (name == "") continue
            if (name !~ /^[A-Za-z0-9_.+-]+$/) { print "Invalid driver name: " name; exit 1 }
            if (name == "VBE20DisplayDriver" && position < 0) position = kept
            if (name != "VBE20DisplayDriver" && name != "FramebufferWC") remaining[++kept] = name
        }
        # Keep VBE in place; put WC directly after VBE, or at the start of the
        # active stage if VBE is a boot driver and WC is not.
        if (position < 0) position = kept
        if (t == 2 && boot[1]) position = 0
        value = ""
        for (i = 0; i <= kept; i++) {
            if (i == position) {
                if (boot[1] == target) value = value " VBE20DisplayDriver"
                if (boot[2] == target) value = value " FramebufferWC"
            }
            if (i < kept) value = value " " remaining[i + 1]
        }
        updated[key] = substr(value, 2)
    }
    text = data[3]; start = 1
    for (i = 1; i <= entries; i++) {
        printf "%s", substr(text, start, first[i] - start) > output
        key = keys[i]
        if (final[key] == i) printf "\"%s\" = \"%s\";", key, updated[key] > output
        start = last[i] + 1
    }
    printf "%s", substr(text, start) > output
    if (!final["Boot Drivers"]) printf "\"Boot Drivers\" = \"%s\";\n", updated["Boot Drivers"] > output
    if (!final["Active Drivers"]) printf "\"Active Drivers\" = \"%s\";\n", updated["Active Drivers"] > output
}' output="$stage/System.edited" table=1 "$stage/VBE20DisplayDriver.new" \
    table=2 "$stage/FramebufferWC.new" table=3 "$stage/System.original" >&2 ||
    fail "Cannot prepare driver activation; no drivers were patched or configured"
cat "$stage/System.edited" > "$stage/System.new" || fail "Cannot stage driver activation"

# The patcher validates hashes, retains a stock backup and is safe to rerun.
patcher="$root/FramebufferWC.config/vbe-cache-patch"
[ -f "$patcher" ] && [ -x "$patcher" ] || fail "Missing executable: $patcher"
echo "Patching VBE for framebuffer write combining..."
"$patcher" patch "$root/VBE20DisplayDriver.config/VBE20DisplayDriver_reloc" ||
    fail "VBE patching failed; driver activation was not changed"

for name in VBE20DisplayDriver FramebufferWC
do
    instance="$root/$name.config/Instance0.table"
    if [ -f "$stage/$name.create" ]; then
        if exists "$instance"; then fail "Driver instance appeared during setup: $instance"; fi
        # A hard link publishes the staged copy without overwriting an instance.
        ln "$stage/$name.new" "$instance" || fail "Cannot create $instance"
        echo "Created $instance"
    else
        # Existing instances are never replaced, even when their defaults differ.
        cmp "$instance" "$stage/$name.original" >/dev/null 2>&1 ||
            fail "Driver instance changed during setup: $instance"
    fi
done
cmp "$system" "$stage/System.original" >/dev/null 2>&1 ||
    fail "System driver configuration changed during setup"
if cmp "$system" "$stage/System.new" >/dev/null 2>&1; then
    :
else
    backup="$system.pre-framebuffer-wc"
    if exists "$backup"; then
        regular "$backup" || fail "Expected a regular backup: $backup"
    else
        ln "$system" "$backup" || fail "Cannot back up System.config"
    fi
    mv "$stage/System.new" "$system" || fail "Cannot replace System.config"
fi
echo "Activated VBE20DisplayDriver followed by FramebufferWC. Reboot to use them."
exit_status=0
