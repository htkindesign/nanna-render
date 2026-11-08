# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Kiet Ho
"""Read-only PE32+ table parser, standard library only. Run its users with python -I.

Keeps the file offset of every DLL name string so patch_imports.py can change exactly those bytes.
"""
import mmap
import struct

IMAGE_SCN_CNT_CODE = 0x00000020
IMAGE_SCN_MEM_EXECUTE = 0x20000000
IMAGE_SCN_MEM_WRITE = 0x80000000
DIR_EXPORT, DIR_IMPORT, DIR_SECURITY, DIR_BOUND, DIR_DELAY = 0, 1, 4, 11, 13


class NotPE(Exception):
    pass


class Section:
    def __init__(self, name, vsize, va, rsize, raw, chars):
        self.name, self.vsize, self.va, self.rsize, self.raw, self.chars = name, vsize, va, rsize, raw, chars

    @property
    def executable(self):
        return bool(self.chars & (IMAGE_SCN_MEM_EXECUTE | IMAGE_SCN_CNT_CODE))

    def contains(self, rva):
        return self.va <= rva < self.va + max(self.vsize, self.rsize)


class PE:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.d = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        d = self.d
        if len(d) < 0x40 or d[:2] != b"MZ":
            raise NotPE(path)
        off = struct.unpack_from("<I", d, 0x3C)[0]
        if off + 24 > len(d) or d[off:off + 4] != b"PE\0\0":
            raise NotPE(path)
        self.machine, nsec, self.timestamp, _, _, optsz, self.chars = struct.unpack_from("<HHIIIHH", d, off + 4)
        o = off + 24
        self.magic = struct.unpack_from("<H", d, o)[0]
        if self.magic != 0x20B:
            raise NotPE(f"{path}: not PE32+ (magic {self.magic:#x})")
        self.linker = (d[o + 2], d[o + 3])
        self.image_base = struct.unpack_from("<Q", d, o + 24)[0]
        self.checksum_offset = o + 64
        n = struct.unpack_from("<I", d, o + 108)[0]
        self.dirs = [struct.unpack_from("<II", d, o + 112 + i * 8) for i in range(n)]
        self.sections = []
        s = o + optsz
        for i in range(nsec):
            name = d[s + i * 40:s + i * 40 + 8].rstrip(b"\0").decode("latin1")
            vsize, va, rsize, raw = struct.unpack_from("<IIII", d, s + i * 40 + 8)
            chars = struct.unpack_from("<I", d, s + i * 40 + 36)[0]
            self.sections.append(Section(name, vsize, va, rsize, raw, chars))

    def close(self):
        self.d.close()

    def directory(self, index):
        return self.dirs[index] if index < len(self.dirs) else (0, 0)

    def section_of(self, rva):
        for s in self.sections:
            if s.contains(rva):
                return s
        return None

    def offset(self, rva):
        s = self.section_of(rva)
        if s is None:
            raise ValueError(f"{self.path}: RVA {rva:#x} is in no section")
        return rva - s.va + s.raw

    def cstr_at(self, offset):
        end = self.d.find(b"\0", offset)
        return self.d[offset:end].decode("latin1")

    def cstr(self, rva):
        return self.cstr_at(self.offset(rva))

    def import_names(self):
        """Yield (table, dll name, file offset of the name string) for every import descriptor.

        table is 'import', 'delay' or 'bound'. A descriptor with several thunks shares one string; two descriptors
        can share one too, so callers should deduplicate on the offset.
        """
        rva, _ = self.directory(DIR_IMPORT)
        if rva:
            o = self.offset(rva)
            while True:
                ilt, _, _, name, iat = struct.unpack_from("<IIIII", self.d, o)
                if not name and not iat and not ilt:
                    break
                yield "import", self.cstr(name), self.offset(name)
                o += 20
        rva, _ = self.directory(DIR_DELAY)
        if rva:
            o = self.offset(rva)
            while True:
                attrs, name = struct.unpack_from("<II", self.d, o)
                if not name:
                    break
                if not attrs & 1:  # old format holds virtual addresses, not RVAs
                    name -= self.image_base
                yield "delay", self.cstr(name), self.offset(name)
                o += 32
        rva, size = self.directory(DIR_BOUND)
        if rva:  # the bound table sits in the headers, so its RVA is also its file offset
            o = rva
            base = o
            while True:
                stamp, name, count = struct.unpack_from("<IHH", self.d, o)
                if not stamp and not name:
                    break
                yield "bound", self.cstr_at(base + name), base + name
                o += 8 + 8 * count

    def imports(self):
        """Return [(dll, by_name, by_ordinal, table)] with one entry per descriptor."""
        out = []
        rva, _ = self.directory(DIR_IMPORT)
        if rva:
            o = self.offset(rva)
            while True:
                ilt, _, _, name, iat = struct.unpack_from("<IIIII", self.d, o)
                if not name and not iat and not ilt:
                    break
                names, ords = self._thunks(ilt or iat, 0)
                out.append((self.cstr(name), names, ords, "import"))
                o += 20
        rva, _ = self.directory(DIR_DELAY)
        if rva:
            o = self.offset(rva)
            while True:
                attrs, name, _, iat, ilt = struct.unpack_from("<IIIII", self.d, o)
                if not name:
                    break
                bias = 0 if attrs & 1 else self.image_base
                names, ords = self._thunks(ilt - bias, bias)
                out.append((self.cstr(name - bias), names, ords, "delay"))
                o += 32
        return out

    def _thunks(self, rva, bias):
        names, ords = [], []
        t = self.offset(rva)
        while True:
            v = struct.unpack_from("<Q", self.d, t)[0]
            if not v:
                return names, ords
            if v >> 63:
                ords.append(v & 0xFFFF)
            else:
                names.append(self.cstr((v - bias & 0x7FFFFFFF) + 2))
            t += 8

    def exports(self):
        """Return (dll name, ordinal base, [(ordinal, name or None, rva, forwarder or None)])."""
        rva, size = self.directory(DIR_EXPORT)
        if not rva:
            return None, 0, []
        o = self.offset(rva)
        name, base, nfuncs, nnames, pfuncs, pnames, pords = struct.unpack_from("<IIIIIII", self.d, o + 12)
        funcs = struct.unpack_from(f"<{nfuncs}I", self.d, self.offset(pfuncs)) if nfuncs else ()
        name_rvas = struct.unpack_from(f"<{nnames}I", self.d, self.offset(pnames)) if nnames else ()
        ords = struct.unpack_from(f"<{nnames}H", self.d, self.offset(pords)) if nnames else ()
        names = {}
        for r, i in zip(name_rvas, ords):
            names.setdefault(i, self.cstr(r))
        out = []
        for i, f in enumerate(funcs):
            if not f:
                continue
            forward = self.cstr(f) if rva <= f < rva + size else None
            out.append((base + i, names.get(i), f, forward))
        return self.cstr(name), base, out


def data_export(pe, rva):
    """True when an export's RVA lies in a section that isn't executable."""
    s = pe.section_of(rva)
    if s is None:
        raise ValueError(f"{pe.path}: export RVA {rva:#x} is in no section")
    return not s.executable


def write_def(pe, library, path):
    """Write a .def for the named exports and return a dict of counts."""
    _, _, exports = pe.exports()
    code = data = quoted = 0
    forwarded, noname, sections = [], [], {}
    lines = []
    for ordinal, name, rva, forward in sorted(exports, key=lambda e: e[1] or ""):
        if name is None:
            noname.append(ordinal)
            continue
        if any(c in name for c in " =;,\""):  # these end a name in a .def file
            if '"' in name:
                raise ValueError(f"export name can't be quoted: {name}")
            name = f'"{name}"'
            quoted += 1
        if forward:
            forwarded.append((name, forward))
            lines.append(f"    {name}")
            code += 1
            continue
        section = pe.section_of(rva)
        sections[section.name] = sections.get(section.name, 0) + 1
        if data_export(pe, rva):
            data += 1
            lines.append(f"    {name} DATA")
        else:
            code += 1
            lines.append(f"    {name}")
    with open(path, "w", encoding="ascii", newline="\n") as f:
        f.write(f"LIBRARY {library}\nEXPORTS\n")
        f.write("\n".join(lines) + "\n")
    return {"total_table_entries": len(exports), "named": len(lines), "code": code, "data": data, "quoted": quoted,
            "forwarded": forwarded, "ordinal_only": noname, "by_section": sections}
