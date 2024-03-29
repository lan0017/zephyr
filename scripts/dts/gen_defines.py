#!/usr/bin/env python3

# Copyright (c) 2019 Nordic Semiconductor ASA
# Copyright (c) 2019 Linaro Limited
# SPDX-License-Identifier: BSD-3-Clause

# This script uses edtlib to generate a header file and a .conf file (both
# containing the same values) from a devicetree (.dts) file. Information from
# binding files in YAML format is used as well.
#
# Bindings are files that describe devicetree nodes. Devicetree nodes are
# usually mapped to bindings via their 'compatible = "..."' property.
#
# See the docstring/comments at the top of edtlib.py for more information.
#
# Note: Do not access private (_-prefixed) identifiers from edtlib here (and
# also note that edtlib is not meant to expose the dtlib API directly).
# Instead, think of what API you need, and add it as a public documented API in
# edtlib. This will keep this script simple.

import argparse
import sys

import edtlib


def main():
    global conf_file
    global header_file

    args = parse_args()

    try:
        edt = edtlib.EDT(args.dts, args.bindings_dirs)
    except edtlib.EDTError as e:
        sys.exit("devicetree error: " + str(e))

    conf_file = open(args.conf_out, "w", encoding="utf-8")
    header_file = open(args.header_out, "w", encoding="utf-8")

    out_comment("Generated by gen_defines.py", blank_before=False)
    out_comment("DTS input file: " + args.dts, blank_before=False)
    out_comment("Directories with bindings: " + ", ".join(args.bindings_dirs),
                blank_before=False)

    active_compats = set()

    for node in edt.nodes:
        if node.enabled and node.matching_compat:
            # Skip 'fixed-partitions' devices since they are handled by
            # write_flash() and would generate extra spurious #defines
            if node.matching_compat == "fixed-partitions":
                continue

            out_comment("Devicetree node: " + node.path)
            out_comment("Binding (compatible = {}): {}".format(
                            node.matching_compat, node.binding_path),
                        blank_before=False)
            out_comment("Binding description: " + node.description,
                        blank_before=False)

            write_regs(node)
            write_irqs(node)
            write_props(node)
            write_clocks(node)
            write_spi_dev(node)
            write_bus(node)
            write_existence_flags(node)

            active_compats.update(node.compats)

    out_comment("Active compatibles (mentioned in DTS + binding found)")
    for compat in sorted(active_compats):
        #define DT_COMPAT_<COMPAT> 1
        out("COMPAT_{}".format(str2ident(compat)), 1)

    # These are derived from /chosen
    write_addr_size(edt, "zephyr,sram", "SRAM")
    write_addr_size(edt, "zephyr,ccm", "CCM")
    write_addr_size(edt, "zephyr,dtcm", "DTCM")

    write_flash(edt.chosen_node("zephyr,flash"))
    write_code_partition(edt.chosen_node("zephyr,code-partition"))

    flash_index = 0
    for node in edt.nodes:
        if node.name.startswith("partition@"):
            write_flash_partition(node, flash_index)
            flash_index += 1

    out_comment("Number of flash partitions")
    if flash_index != 0:
        out("FLASH_AREA_NUM", flash_index)

    print("Devicetree configuration written to " + args.conf_out)


def parse_args():
    # Returns parsed command-line arguments

    parser = argparse.ArgumentParser()
    parser.add_argument("--dts", required=True, help="DTS file")
    parser.add_argument("--bindings-dirs", nargs='+', required=True,
                        help="directory with bindings in YAML format, "
                        "we allow multiple")
    parser.add_argument("--header-out", required=True,
                        help="path to write header to")
    parser.add_argument("--conf-out", required=True,
                        help="path to write configuration file to")

    return parser.parse_args()


def write_regs(node):
    # Writes address/size output for the registers in the node's 'reg' property

    def reg_addr_name_alias(reg):
        return str2ident(reg.name) + "_BASE_ADDRESS" if reg.name else None

    def reg_size_name_alias(reg):
        return str2ident(reg.name) + "_SIZE" if reg.name else None

    for reg in node.regs:
        out_dev(node, reg_addr_ident(reg), hex(reg.addr),
                name_alias=reg_addr_name_alias(reg))

        if reg.size:
            out_dev(node, reg_size_ident(reg), reg.size,
                    name_alias=reg_size_name_alias(reg))


def write_props(node):
    # Writes any properties defined in the "properties" section of the binding
    # for the node

    for prop in node.props.values():
        # Skip #size-cell and other property starting with #. Also skip mapping
        # properties like 'gpio-map'.
        if prop.name[0] == "#" or prop.name.endswith("-map"):
            continue

        # See write_clocks()
        if prop.name == "clocks":
            continue

        # edtlib provides these as well (Property.val becomes an edtlib.Node
        # and a list of edtlib.Nodes, respectively). Nothing is generated for
        # them currently though.
        if prop.type in {"phandle", "phandles"}:
            continue

        # Skip properties that we handle elsewhere
        if prop.name in {
            "reg", "compatible", "status", "interrupts",
            "interrupt-controller", "gpio-controller"
        }:
            continue

        if prop.description is not None:
            out_comment(prop.description, blank_before=False)

        ident = str2ident(prop.name)

        if prop.type == "boolean":
            out_dev(node, ident, 1 if prop.val else 0)
        elif prop.type == "string":
            out_dev_s(node, ident, prop.val)
        elif prop.type == "int":
            out_dev(node, ident, prop.val)
        elif prop.type == "array":
            for i, val in enumerate(prop.val):
                out_dev(node, "{}_{}".format(ident, i), val)
        elif prop.type == "string-array":
            for i, val in enumerate(prop.val):
                out_dev_s(node, "{}_{}".format(ident, i), val)
        elif prop.type == "uint8-array":
            out_dev(node, ident,
                    "{ " + ", ".join("0x{:02x}".format(b) for b in prop.val) + " }")
        elif prop.type == "phandle-array":
            write_phandle_val_list(prop)

        # Generate DT_..._ENUM if there's an 'enum:' key in the binding
        if prop.enum_index is not None:
            out_dev(node, ident + "_ENUM", prop.enum_index)


def write_bus(node):
    # Generate bus-related #defines

    if not node.bus:
        return

    if node.parent.label is None:
        err("missing 'label' property on {!r}".format(node.parent))

    # #define DT_<DEV-IDENT>_BUS_NAME <BUS-LABEL>
    out_dev_s(node, "BUS_NAME", str2ident(node.parent.label))

    for compat in node.compats:
        # #define DT_<COMPAT>_BUS_<BUS-TYPE> 1
        out("{}_BUS_{}".format(str2ident(compat), str2ident(node.bus)), 1)


def write_existence_flags(node):
    # Generate #defines of the form
    #
    #   #define DT_INST_<INSTANCE>_<COMPAT> 1
    #
    # These are flags for which devices exist.

    for compat in node.compats:
        out("INST_{}_{}".format(node.instance_no[compat],
                                str2ident(compat)), 1)


def reg_addr_ident(reg):
    # Returns the identifier (e.g., macro name) to be used for the address of
    # 'reg' in the output

    node = reg.node

    # NOTE: to maintain compat wit the old script we special case if there's
    # only a single register (we drop the '_0').
    if len(node.regs) > 1:
        return "BASE_ADDRESS_{}".format(node.regs.index(reg))
    else:
        return "BASE_ADDRESS"


def reg_size_ident(reg):
    # Returns the identifier (e.g., macro name) to be used for the size of
    # 'reg' in the output

    node = reg.node

    # NOTE: to maintain compat wit the old script we special case if there's
    # only a single register (we drop the '_0').
    if len(node.regs) > 1:
        return "SIZE_{}".format(node.regs.index(reg))
    else:
        return "SIZE"


def dev_ident(node):
    # Returns an identifier for the device given by 'node'. Used when building
    # e.g. macro names.

    # TODO: Handle PWM on STM
    # TODO: Better document the rules of how we generate things

    ident = ""

    if node.bus:
        ident += "{}_{:X}_".format(
            str2ident(node.parent.matching_compat), node.parent.unit_addr)

    ident += "{}_".format(str2ident(node.matching_compat))

    if node.unit_addr is not None:
        ident += "{:X}".format(node.unit_addr)
    elif node.parent.unit_addr is not None:
        ident += "{:X}_{}".format(node.parent.unit_addr, str2ident(node.name))
    else:
        # This is a bit of a hack
        ident += "{}".format(str2ident(node.name))

    return ident


def dev_aliases(node):
    # Returns a list of aliases for the device given by 'node', used e.g. when
    # building macro names

    return dev_path_aliases(node) + dev_instance_aliases(node)


def dev_path_aliases(node):
    # Returns a list of aliases for the device given by 'node', based on the
    # aliases registered for it, in the /aliases node. Used when building e.g.
    # macro names.

    if node.matching_compat is None:
        return []

    compat_s = str2ident(node.matching_compat)

    aliases = []
    for alias in node.aliases:
        aliases.append("ALIAS_{}".format(str2ident(alias)))
        # TODO: See if we can remove or deprecate this form
        aliases.append("{}_{}".format(compat_s, str2ident(alias)))

    return aliases


def dev_instance_aliases(node):
    # Returns a list of aliases for the device given by 'node', based on the
    # instance number of the device (based on how many instances of that
    # particular device there are).
    #
    # This is a list since a device can have multiple 'compatible' strings,
    # each with their own instance number.

    return ["INST_{}_{}".format(node.instance_no[compat], str2ident(compat))
            for compat in node.compats]


def write_addr_size(edt, prop_name, prefix):
    # Writes <prefix>_BASE_ADDRESS and <prefix>_SIZE for the node pointed at by
    # the /chosen property named 'prop_name', if it exists

    node = edt.chosen_node(prop_name)
    if not node:
        return

    if not node.regs:
        err("missing 'reg' property in node pointed at by /chosen/{} ({!r})"
            .format(prop_name, node))

    out_comment("/chosen/{} ({})".format(prop_name, node.path))
    out("{}_BASE_ADDRESS".format(prefix), hex(node.regs[0].addr))
    out("{}_SIZE".format(prefix), node.regs[0].size//1024)


def write_flash(flash_node):
    # Writes output for the node pointed at by the zephyr,flash property in
    # /chosen

    out_comment("/chosen/zephyr,flash ({})"
                .format(flash_node.path if flash_node else "missing"))

    if not flash_node:
        # No flash node. Write dummy values.
        out("FLASH_BASE_ADDRESS", 0)
        out("FLASH_SIZE", 0)
        return

    if len(flash_node.regs) != 1:
        err("expected zephyr,flash to have a single register, has {}"
            .format(len(flash_node.regs)))

    if flash_node.bus == "spi" and len(flash_node.parent.regs) == 2:
        reg = flash_node.parent.regs[1]  # QSPI flash
    else:
        reg = flash_node.regs[0]

    out("FLASH_BASE_ADDRESS", hex(reg.addr))
    if reg.size:
        out("FLASH_SIZE", reg.size//1024)

    if "erase-block-size" in flash_node.props:
        out("FLASH_ERASE_BLOCK_SIZE", flash_node.props["erase-block-size"].val)

    if "write-block-size" in flash_node.props:
        out("FLASH_WRITE_BLOCK_SIZE", flash_node.props["write-block-size"].val)


def write_code_partition(code_node):
    # Writes output for the node pointed at by the zephyr,code-partition
    # property in /chosen

    out_comment("/chosen/zephyr,code-partition ({})"
                .format(code_node.path if code_node else "missing"))

    if not code_node:
        # No code partition. Write dummy values.
        out("CODE_PARTITION_OFFSET", 0)
        out("CODE_PARTITION_SIZE", 0)
        return

    if not code_node.regs:
        err("missing 'regs' property on {!r}".format(code_node))

    out("CODE_PARTITION_OFFSET", code_node.regs[0].addr)
    out("CODE_PARTITION_SIZE", code_node.regs[0].size)


def write_flash_partition(partition_node, index):
    out_comment("Flash partition at " + partition_node.path)

    if partition_node.label is None:
        err("missing 'label' property on {!r}".format(partition_node))

    # Generate label-based identifiers
    write_flash_partition_prefix(
        "FLASH_AREA_" + str2ident(partition_node.label), partition_node, index)

    # Generate index-based identifiers
    write_flash_partition_prefix(
        "FLASH_AREA_{}".format(index), partition_node, index)


def write_flash_partition_prefix(prefix, partition_node, index):
    # write_flash_partition() helper. Generates identifiers starting with
    # 'prefix'.

    out("{}_ID".format(prefix), index)

    out("{}_READ_ONLY".format(prefix), 1 if partition_node.read_only else 0)

    for i, reg in enumerate(partition_node.regs):
        # Also add aliases that point to the first sector (TODO: get rid of the
        # aliases?)
        out("{}_OFFSET_{}".format(prefix, i), reg.addr,
            aliases=["{}_OFFSET".format(prefix)] if i == 0 else [])
        out("{}_SIZE_{}".format(prefix, i), reg.size,
            aliases=["{}_SIZE".format(prefix)] if i == 0 else [])

    controller = partition_node.flash_controller
    if controller.label is not None:
        out_s("{}_DEV".format(prefix), controller.label)


def write_irqs(node):
    # Writes IRQ num and data for the interrupts in the node's 'interrupt'
    # property

    def irq_name_alias(irq, cell_name):
        if not irq.name:
            return None

        alias = "IRQ_{}".format(str2ident(irq.name))
        if cell_name != "irq":
            alias += "_" + str2ident(cell_name)
        return alias

    def encode_zephyr_multi_level_irq(irq, irq_num):
        # See doc/reference/kernel/other/interrupts.rst for details
        # on how this encoding works

        irq_ctrl = irq.controller
        # Look for interrupt controller parent until we have none
        while irq_ctrl.interrupts:
            irq_num = (irq_num + 1) << 8
            if "irq" not in irq_ctrl.interrupts[0].data:
                err("Expected binding for {!r} to have 'irq' "
                    "in '#cells'".format(irq_ctrl))
            irq_num |= irq_ctrl.interrupts[0].data["irq"]
            irq_ctrl = irq_ctrl.interrupts[0].controller
        return irq_num

    for irq_i, irq in enumerate(node.interrupts):
        for cell_name, cell_value in irq.data.items():
            ident = "IRQ_{}".format(irq_i)
            if cell_name == "irq":
                cell_value = encode_zephyr_multi_level_irq(irq, cell_value)
            else:
                ident += "_" + str2ident(cell_name)

            out_dev(node, ident, cell_value,
                    name_alias=irq_name_alias(irq, cell_name))


def write_spi_dev(node):
    # Writes SPI device GPIO chip select data if there is any

    cs_gpio = edtlib.spi_dev_cs_gpio(node)
    if cs_gpio is not None:
        write_phandle_val_list_entry(node, cs_gpio, None, "CS_GPIOS")


def write_phandle_val_list(prop):
    # Writes output for a phandle/value list, e.g.
    #
    #    pwms = <&pwm-ctrl-1 10 20
    #            &pwm-ctrl-2 30 40>;
    #
    # prop:
    #   phandle/value Property instance.
    #
    #   If only one entry appears in 'prop' (the example above has two), the
    #   generated identifier won't get a '_0' suffix, and the '_COUNT' and
    #   group initializer are skipped too.
    #
    # The base identifier is derived from the property name. For example, 'pwms = ...'
    # generates output like this:
    #
    #   #define <device prefix>_PWMS_CONTROLLER_0 "PWM_0"  (name taken from 'label = ...')
    #   #define <device prefix>_PWMS_CHANNEL_0 123         (name taken from #cells in binding)
    #   #define <device prefix>_PWMS_0 {"PWM_0", 123}
    #   #define <device prefix>_PWMS_CONTROLLER_1 "PWM_1"
    #   #define <device prefix>_PWMS_CHANNEL_1 456
    #   #define <device prefix>_PWMS_1 {"PWM_1", 456}
    #   #define <device prefix>_PWMS_COUNT 2
    #   #define <device prefix>_PWMS {<device prefix>_PWMS_0, <device prefix>_PWMS_1}
    #   ...

    # pwms -> PWMS
    # foo-gpios -> FOO_GPIOS
    ident = str2ident(prop.name)

    initializer_vals = []
    for i, entry in enumerate(prop.val):
        initializer_vals.append(write_phandle_val_list_entry(
            prop.node, entry, i if len(prop.val) > 1 else None, ident))

    if len(prop.val) > 1:
        out_dev(prop.node, ident + "_COUNT", len(initializer_vals))
        out_dev(prop.node, ident, "{" + ", ".join(initializer_vals) + "}")


def write_phandle_val_list_entry(node, entry, i, ident):
    # write_phandle_val_list() helper. We could get rid of it if it wasn't for
    # write_spi_dev(). Adds 'i' as an index to identifiers unless it's None.
    #
    # 'entry' is an edtlib.ControllerAndData instance.
    #
    # Returns the identifier for the macro that provides the
    # initializer for the entire entry.

    initializer_vals = []
    if entry.controller.label is not None:
        ctrl_ident = ident + "_CONTROLLER"  # e.g. PWMS_CONTROLLER
        if entry.name:
            ctrl_ident = str2ident(entry.name) + "_" + ctrl_ident
        # Ugly backwards compatibility hack. Only add the index if there's
        # more than one entry.
        if i is not None:
            ctrl_ident += "_{}".format(i)
        initializer_vals.append(quote_str(entry.controller.label))
        out_dev_s(node, ctrl_ident, entry.controller.label)

    for cell, val in entry.data.items():
        cell_ident = ident + "_" + str2ident(cell)  # e.g. PWMS_CHANNEL
        if entry.name:
            # From e.g. 'pwm-names = ...'
            cell_ident = str2ident(entry.name) + "_" + cell_ident
        # Backwards compatibility (see above)
        if i is not None:
            cell_ident += "_{}".format(i)
        out_dev(node, cell_ident, val)

    initializer_vals += entry.data.values()

    initializer_ident = ident
    if entry.name:
        initializer_ident += "_" + str2ident(entry.name)
    if i is not None:
        initializer_ident += "_{}".format(i)
    return out_dev(node, initializer_ident,
                   "{" + ", ".join(map(str, initializer_vals)) + "}")


def write_clocks(node):
    # Writes clock information.
    #
    # Most of this ought to be handled in write_props(), but the identifiers
    # that get generated for 'clocks' are inconsistent with the with other
    # 'phandle-array' properties.
    #
    # See https://github.com/zephyrproject-rtos/zephyr/pull/19327#issuecomment-534081845.

    if "clocks" not in node.props:
        return

    for clock_i, clock in enumerate(node.props["clocks"].val):
        controller = clock.controller

        if controller.label is not None:
            out_dev_s(node, "CLOCK_CONTROLLER", controller.label)

        for name, val in clock.data.items():
            if clock_i == 0:
                clk_name_alias = "CLOCK_" + str2ident(name)
            else:
                clk_name_alias = None

            out_dev(node, "CLOCK_{}_{}".format(str2ident(name), clock_i), val,
                    name_alias=clk_name_alias)

        if "fixed-clock" not in controller.compats:
            continue

        if "clock-frequency" not in controller.props:
            err("{!r} is a 'fixed-clock' but lacks a 'clock-frequency' "
                "property".format(controller))

        out_dev(node, "CLOCKS_CLOCK_FREQUENCY",
                controller.props["clock-frequency"].val)


def str2ident(s):
    # Converts 's' to a form suitable for (part of) an identifier

    return s.replace("-", "_") \
            .replace(",", "_") \
            .replace("@", "_") \
            .replace("/", "_") \
            .replace(".", "_") \
            .replace("+", "PLUS") \
            .upper()


def out_dev(node, ident, val, name_alias=None):
    # Writes an
    #
    #   <device prefix>_<ident> = <val>
    #
    # assignment, along with a set of
    #
    #   <device alias>_<ident>
    #
    # aliases, for each device alias. If 'name_alias' (a string) is passed,
    # then these additional aliases are generated:
    #
    #   <device prefix>_<name alias>
    #   <device alias>_<name alias> (for each device alias)
    #
    # 'name_alias' is used for reg-names and the like.
    #
    # Returns the identifier used for the macro that provides the value
    # for 'ident' within 'node', e.g. DT_MFG_MODEL_CTL_GPIOS_PIN.

    dev_prefix = dev_ident(node)

    aliases = [alias + "_" + ident for alias in dev_aliases(node)]
    if name_alias is not None:
        aliases.append(dev_prefix + "_" + name_alias)
        aliases += [alias + "_" + name_alias for alias in dev_aliases(node)]

    return out(dev_prefix + "_" + ident, val, aliases)


def out_dev_s(node, ident, s):
    # Like out_dev(), but emits 's' as a string literal
    #
    # Returns the generated macro name for 'ident'.

    return out_dev(node, ident, quote_str(s))


def out_s(ident, val):
    # Like out(), but puts quotes around 'val' and escapes any double
    # quotes and backslashes within it
    #
    # Returns the generated macro name for 'ident'.

    return out(ident, quote_str(val))


def out(ident, val, aliases=()):
    # Writes '#define <ident> <val>' to the header and '<ident>=<val>' to the
    # the configuration file.
    #
    # Also writes any aliases listed in 'aliases' (an iterable). For the
    # header, these look like '#define <alias> <ident>'. For the configuration
    # file, the value is just repeated as '<alias>=<val>' for each alias.
    #
    # Returns the generated macro name for 'ident'.

    print("#define DT_{:40} {}".format(ident, val), file=header_file)
    primary_ident = "DT_{}".format(ident)

    # Exclude things that aren't single token values from .conf.  At
    # the moment the only such items are unquoted string
    # representations of initializer lists, which begin with a curly
    # brace.
    output_to_conf = not (isinstance(val, str) and val.startswith("{"))
    if output_to_conf:
        print("{}={}".format(primary_ident, val), file=conf_file)

    for alias in aliases:
        if alias != ident:
            print("#define DT_{:40} DT_{}".format(alias, ident),
                  file=header_file)
            if output_to_conf:
                # For the configuration file, the value is just repeated for all
                # the aliases
                print("DT_{}={}".format(alias, val), file=conf_file)

    return primary_ident


def out_comment(s, blank_before=True):
    # Writes 's' as a comment to the header and configuration file. 's' is
    # allowed to have multiple lines. blank_before=True adds a blank line
    # before the comment.

    if blank_before:
        print(file=header_file)
        print(file=conf_file)

    # Double-space in header for readability
    print("/*  " + s + "  */", file=header_file)
    print("\n".join("# " + line for line in s.splitlines()), file=conf_file)


def escape(s):
    # Backslash-escapes any double quotes and backslashes in 's'

    # \ must be escaped before " to avoid double escaping
    return s.replace("\\", "\\\\").replace('"', '\\"')


def quote_str(s):
    # Puts quotes around 's' and escapes any double quotes and
    # backslashes within it

    return '"{}"'.format(escape(s))


def err(s):
    raise Exception(s)


if __name__ == "__main__":
    main()
