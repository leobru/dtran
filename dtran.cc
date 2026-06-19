/*
 * A disassembler for BESM-6 binaries, combining three input formats:
 *
 *   - DMS      : Dubna Monitor System object files (packed or unpacked
 *                header, optional magic key + entry table, symbol table,
 *                DATA/SET sections).            [was dtran2.cc / dtran3.cc]
 *   - Pascal-A : Pascal-Autocode executable, load base 0, total length in
 *                memory[5], entry in memory[010], GOST text.   [was dtran1.cc]
 *   - Pascal-B : Pascal-Autocode executable, load base 02000, paged length,
 *                GOST/ITM/ISO/TEXT with auto-detection and a Pascal-monitor
 *                symbol table.                                 [was dtran4.cc]
 *
 * The format is auto-detected and can be forced with -F dms|pa|pb.
 *
 * To produce pseudo-code that can be given to the "decompiler":
 *   dtran -d file.o > file.out
 * To produce assembly re-assemblable with *MADLEN (DMS only):
 *   dtran -e -l file.o > file.asm
 *
 * Copyright 2017-2019 Leonid Broukhis
 */

#include <cstdio>
#include <cstdint>
#include <cstring>
#include <strings.h>
#include <cstdlib>
#include <cstdarg>
#include <string>
#include <vector>
#include <map>
#include <set>
#include <algorithm>
#include <sys/stat.h>
#include "unistd.h"

/*
 * First word of a DMS object file: magic key.
 */
const uint64_t BESM6_MAGIC = 0x4245534d3600;

/*
 * BESM-6 opcode types.
 */
typedef enum {
OPCODE_ILLEGAL,
OPCODE_STR1,		/* short addr */
OPCODE_STR2,		/* long addr */
OPCODE_IMM,		/* e.g. NTR */
OPCODE_REG1,		/* e.g. ATI */
OPCODE_JUMP,		/* UJ */
OPCODE_BRANCH,		/* UZA, U1A, VZM, V1M, VLM */
OPCODE_CALL,		/* VJM */
OPCODE_IMM64,		/* e.g. ASN */
OPCODE_ADDRMOD,		/* UTC, WTC */
OPCODE_REG2,		/* VTM, UTM */
OPCODE_IMMEX,		/* *50, ... */
OPCODE_ADDREX,		/* *64, *70, ... */
OPCODE_STOP,		/* *74 */
OPCODE_DEFAULT
} opcode_e;

struct opcode {
	const char *name;
	unsigned opcode;
	unsigned mask;
	opcode_e type;
} op[] = {
  /* name,	pattern,  mask,	    opcode type */
  { "ATX",	0x000000, 0x0bf000, OPCODE_STR1 },
  { "STX",	0x001000, 0x0bf000, OPCODE_STR1 },
  { "XTS",	0x003000, 0x0bf000, OPCODE_STR1 },
  { "A+X",	0x004000, 0x0bf000, OPCODE_STR1 },
  { "A-X",	0x005000, 0x0bf000, OPCODE_STR1 },
  { "X-A",	0x006000, 0x0bf000, OPCODE_STR1 },
  { "AMX",	0x007000, 0x0bf000, OPCODE_STR1 },
  { "XTA",	0x008000, 0x0bf000, OPCODE_STR1 },
  { "AAX",	0x009000, 0x0bf000, OPCODE_STR1 },
  { "AEX",	0x00a000, 0x0bf000, OPCODE_STR1 },
  { "ARX",	0x00b000, 0x0bf000, OPCODE_STR1 },
  { "AVX",	0x00c000, 0x0bf000, OPCODE_STR1 },
  { "AOX",	0x00d000, 0x0bf000, OPCODE_STR1 },
  { "A/X",	0x00e000, 0x0bf000, OPCODE_STR1 },
  { "A*X",	0x00f000, 0x0bf000, OPCODE_STR1 },
  { "APX",	0x010000, 0x0bf000, OPCODE_STR1 },
  { "AUX",	0x011000, 0x0bf000, OPCODE_STR1 },
  { "ACX",	0x012000, 0x0bf000, OPCODE_STR1 },
  { "ANX",	0x013000, 0x0bf000, OPCODE_STR1 },
  { "E+X",	0x014000, 0x0bf000, OPCODE_STR1 },
  { "E-X",	0x015000, 0x0bf000, OPCODE_STR1 },
  { "ASX",	0x016000, 0x0bf000, OPCODE_STR1 },
  { "XTR",	0x017000, 0x0bf000, OPCODE_STR1 },
  { "RTE",	0x018000, 0x0bf000, OPCODE_IMM },
  { "YTA",	0x019000, 0x0bf000, OPCODE_IMM64 },
  { "E+N",	0x01c000, 0x0bf000, OPCODE_IMM64 },
  { "E-N",	0x01d000, 0x0bf000, OPCODE_IMM64 },
  { "ASN",	0x01e000, 0x0bf000, OPCODE_IMM64 },
  { "NTR",	0x01f000, 0x0bf000, OPCODE_IMM },
  { "ATI",	0x020000, 0x0bf000, OPCODE_REG1 },
  { "STI",	0x021000, 0x0bf000, OPCODE_REG1 },
  { "ITA",	0x022000, 0x0bf000, OPCODE_REG1 },
  { "ITS",	0x023000, 0x0bf000, OPCODE_REG1 },
  { "MTJ",	0x024000, 0x0bf000, OPCODE_REG1 },
  { "J+M",	0x025000, 0x0bf000, OPCODE_REG1 },
  { "*50",	0x028000, 0x0bf000, OPCODE_IMMEX },
  { "*51",	0x029000, 0x0bf000, OPCODE_IMMEX },
  { "*52",	0x02a000, 0x0bf000, OPCODE_IMMEX },
  { "*53",	0x02b000, 0x0bf000, OPCODE_IMMEX },
  { "*54",	0x02c000, 0x0bf000, OPCODE_IMMEX },
  { "*55",	0x02d000, 0x0bf000, OPCODE_IMMEX },
  { "*56",	0x02e000, 0x0bf000, OPCODE_IMMEX },
  { "*57",	0x02f000, 0x0bf000, OPCODE_IMMEX },
  { "*60",	0x030000, 0x0bf000, OPCODE_ADDREX },
  { "*61",	0x031000, 0x0bf000, OPCODE_ADDREX },
  { "*62",	0x032000, 0x0bf000, OPCODE_IMMEX },
  { "*63",	0x033000, 0x0bf000, OPCODE_IMMEX },
  { "*64",	0x034000, 0x0bf000, OPCODE_ADDREX },
  { "*65",	0x035000, 0x0bf000, OPCODE_IMMEX },
  { "*66",	0x036000, 0x0bf000, OPCODE_IMMEX },
  { "*67",	0x037000, 0x0bf000, OPCODE_ADDREX },
  { "*70",	0x038000, 0x0bf000, OPCODE_ADDREX },
  { "*71",	0x039000, 0x0bf000, OPCODE_ADDREX },
  { "*72",	0x03a000, 0x0bf000, OPCODE_ADDREX },
  { "*73",	0x03b000, 0x0bf000, OPCODE_ADDREX },
  { "*74",	0x03c000, 0x0bf000, OPCODE_STOP },
  { "CTX",	0x03d000, 0x0bf000, OPCODE_ADDREX },
  { "*76",	0x03e000, 0x0bf000, OPCODE_IMMEX },
  { "*77",	0x03f000, 0x0bf000, OPCODE_IMMEX },
  { "UTC",	0x090000, 0x0f8000, OPCODE_ADDRMOD },
  { "WTC",	0x098000, 0x0f8000, OPCODE_ADDRMOD },
  { "VTM",	0x0a0000, 0x0f8000, OPCODE_REG2 },
  { "UTM",	0x0a8000, 0x0f8000, OPCODE_REG2 },
  { "UZA",	0x0b0000, 0x0f8000, OPCODE_BRANCH },
  { "U1A",	0x0b8000, 0x0f8000, OPCODE_BRANCH },
  { "UJ",	0x0c0000, 0x0f8000, OPCODE_JUMP },
  { "VJM",	0x0c8000, 0x0f8000, OPCODE_CALL },
  { "VZM",	0x0e0000, 0x0f8000, OPCODE_BRANCH },
  { "V1M",	0x0e8000, 0x0f8000, OPCODE_BRANCH },
  { "VLM",	0x0f8000, 0x0f8000, OPCODE_BRANCH },
/* This entry MUST be last; it is a "catch-all" entry that will match when no
 * other opcode entry matches during disassembly.
 */
  { "",		0x0000, 0x0000, OPCODE_ILLEGAL },
};

typedef unsigned long long uint64;
typedef unsigned int uint32;
typedef unsigned int uint;

std::string strprintf(const char * fmt, ...) {
    std::string ret;
    char * str;
    va_list ap;
    va_start (ap, fmt);
    if (vasprintf(&str, fmt, ap) < 0) {
        fprintf(stderr, "Out of memory\n");
        exit(1);
    }
    va_end(ap);
    ret = str;
    free(str);
    return ret;
}

/* GOST 8-bit text encoding (Pascal-Autocode binaries). */
static const char * gost_to_utf[] = {
    "0", "1", "2", "3", "4", "5", "6", "7",
    "8", "9", "+", "-", "/", ",", ".", " ",
    "⏨", "↑", "(", ")", "×", "=", ";", "[",
    "]", "*", "`", "'", "#", "<", ">", ":",
    "А", "Б", "В", "Г", "Д", "Е", "Ж", "З",
    "И", "Й", "К", "Л", "М", "Н", "О", "П",
    "Р", "С", "Т", "У", "Ф", "Х", "Ц", "Ч",
    "Ш", "Щ", "Ы", "Ь", "Э", "Ю", "Я", "D",
    "F", "G", "I", "J", "L", "N", "Q", "R",
    "S", "U", "V", "W", "Z", "^", "≤", "≥",
    "∨", "&", "⊃", "~", "÷", "≡", "%", "$",
    "|", "—", "_", "!", "\"", "Ъ", "?", "′"
};

/* 6-bit TEXT encoding (DMS object files and Pascal-B). */
static const char * text_to_utf[] = {
    " ", ".", "Б", "Ц", "Д", "Ф", "Г", "И",
    "(", ")", "*", "Й", "Л", "Я", "Ж", "/",
    "0", "1", "2", "3", "4", "5", "6", "7",
    "8", "9", "Ь", ",", "П", "-", "+", "Ы",
    "З", "A", "B", "C", "D", "E", "F", "G",
    "H", "I", "J", "K", "L", "M", "N", "O",
    "P", "Q", "R", "S", "T", "U", "V", "W",
    "X", "Y", "Z", "Ш", "Э", "Щ", "Ч", "Ю"
};

/* GOST -> ITM (telegraph) mapping for ITM literal detection (Pascal-B). */
const unsigned char gost_to_itm [0140] =
{
/* 000-007 */   0000,   0001,   0002,   0003,   0004,   0005,   0006,   0007,
/* 010-017 */   0010,   0011,   0061,   0070,   0067,   0046,   0047,   0017,
/* 020-027 */   0176,   0143,   0076,   0051,   0072,   0057,   0045,   0053,
/* 030-037 */   0066,   0170,   0064,   0041,   0152,   0074,   0054,   0056,
/* 040-047 */   0230,   0323,   0223,   0313,   0322,   0220,   0317,   0321,
/* 050-057 */   0314,   0332,   0236,   0311,   0207,   0205,   0203,   0315,
/* 060-067 */   0215,   0216,   0201,   0225,   0326,   0227,   0316,   0331,
/* 070-077 */   0324,   0301,   0325,   0327,   0320,   0334,   0335,   0222,
/* 100-107 */   0226,   0213,   0214,   0232,   0211,   0206,   0235,   0212,
/* 110-117 */   0224,   0234,   0217,   0231,   0221,   0050,   0074,   0054,
/* 120-127 */   0060,   0071,   0055,   0145,   0065,   0075,   0062,   0042,
/* 130-137 */   0044,   0150,   0043,   0063,   0134,   0136,   0   ,   0   ,
};

const char * itm_to_utf[256];

void populate_itm() {
    itm_to_utf[0] = gost_to_utf[0];
    for (int i = 1; i < 0140; ++i) {
        if (gost_to_itm[i] == 0)
            continue;
        if (itm_to_utf[gost_to_itm[i]])
            fprintf(stderr, "GOST %03o maps to the same ITM as GOST %s\n",
                    i, itm_to_utf[gost_to_itm[i]]);
        else
            itm_to_utf[gost_to_itm[i]] = gost_to_utf[i];
    }
}

FILE * entries;
std::set<int> gostoff, itmoff, isooff, textoff, codeoff;
int forced_code_off;

enum Format { FMT_AUTO, FMT_DMS, FMT_PASCAL_A, FMT_PASCAL_B };

/*
 * Read a 48-bit word from a 6-byte big-endian group.
 */
static uint64 freadw (FILE *fd) {
    uint64 val = 0;
    for (int i = 0; i < 6; ++i) {
        val <<= 8;
        val |= getc (fd);
    }
    return val;
}

/*
 * Decide the input format from the raw file words. Each candidate is accepted
 * only if its header is self-consistent against the word count, so the checks
 * do not rely on a single ambiguous bit.
 */
static Format detect_format(const std::vector<uint64> & raw) {
    size_t n = raw.size();
    if (n > 0 && raw[0] == BESM6_MAGIC)
        return FMT_DMS;

    // DMS: packed or unpacked header whose computed extent fits the file.
    if (n > 3) {
        uint head_off = 0;
        uint head_len, sym_len, debug_len, data_len, set_len, long_len, cmd_len, const_len;
        bool ok = true;
        if ((raw[1] >> 45) != 0) {
            // Unpacked header (skip entry table).
            while (head_off + 1 < n && (raw[head_off + 1] >> 45) != 0)
                head_off += 2;
            if (head_off + 10 > n) ok = false;
            else {
                head_len  = raw[head_off];
                sym_len   = raw[head_off + 1];
                debug_len = raw[head_off + 3];
                long_len  = raw[head_off + 4];
                cmd_len   = raw[head_off + 5];
                const_len = raw[head_off + 7];
                data_len  = raw[head_off + 8];
                set_len   = raw[head_off + 9];
            }
        } else {
            head_len = raw[0] & 07777;
            sym_len = (raw[0] >> 12) & 07777;
            debug_len = (raw[0] >> 36);
            set_len = raw[1] & 077777;
            data_len = (raw[1] >> 15) & 077777;
            long_len = (raw[1] >> 30) & 077777;
            cmd_len = raw[2] & 077777;
            const_len = (raw[2] >> 30) & 077777;
        }
        if (ok && cmd_len > 0 && cmd_len < 32768) {
            uint64 cmd_off = (raw[1] >> 45) != 0 ? head_off + 10 : 3;
            uint64 comment_off = cmd_off + cmd_len + const_len + data_len + set_len
                                 + head_len + sym_len + long_len + debug_len;
            if (comment_off > cmd_off && comment_off <= n)
                return FMT_DMS;
        }
    }

    // Pascal-A: total length in memory[5], entry in memory[010].
    if (n > 010) {
        uint total = raw[5] & 077777;
        uint main_off = raw[010] & 077777;
        if (total > 0 && total < 32768 && total <= n &&
            main_off > 0 && main_off < total)
            return FMT_PASCAL_A;
    }

    // Pascal-B: paged length from memory[02010]/[02011] (file words 8/9).
    if (n > 9) {
        uint total = ((raw[9] && raw[9] < 037) ? raw[9] : raw[8]) * 02000 + 02000;
        if (total > 02000 && total - 02000 <= n && total < 32768)
            return FMT_PASCAL_B;
    }

    return FMT_AUTO;            // undecided
}

struct Dtran {
    Format fmt;

    // DMS header.
    uint head_len, sym_len, debug_len, data_len, set_len, long_len;
    uint cmd_len, bss_len, const_len;
    uint head_off, cmd_off, table_off, debug_off, long_off, comment_off;

    // Pascal header.
    uint total_len, main_off, code_off, code_len;

    uint basereg, baseaddr, baseop;
    bool nolabels, noequs, nooctal, nodlabels;

    uint64 memory[32768];
    bool code_map[32768];
    enum fmt_e { fLOG, fINT, fGOST, fISO, fTEXT, fITM } format_map[32768];

    std::vector<std::string> symtab;
    std::vector<std::string> labels;

    // ------------------------------------------------------------------
    // Shared helpers
    // ------------------------------------------------------------------
    void mklabel(uint off) {
        labels[off] = strprintf("L%04o", off);
    }

    int get_opidx(uint32 opcode) {
        int i = -1;
        do {
            i = i + 1;
            if ((opcode & op[i].mask) == op[i].opcode)
                return i;
        } while (op[i].mask);
        return -1;
    }

    std::string get_utf8(uint unic) {
        std::string ret;
        if (unic < 0x80) {
            ret = char(unic);
        } else if (unic < 0x800) {
            ret = char(unic >> 6 | 0xc0);
            ret += char((unic & 0x3f) | 0x80);
        } else {
            ret = char(unic >> 12 | 0xe0);
            ret += char(((unic >> 6) & 0x3f) | 0x80);
            ret += char ((unic & 0x3f) | 0x80);
        }
        return ret;
    }

    std::string get_gost_char (unsigned char ch) {
        if (ch < 0140)
            return gost_to_utf[ch];
        return strprintf("_%03o", ch);
    }
    std::string get_gost_word(uint64 word) {
        std::string ret;
        for (uint i = 40; i <= 40; i-=8)
            ret += get_gost_char(word >> i);
        return ret;
    }

    std::string get_itm_char (unsigned char ch) {
        return itm_to_utf[ch] ? itm_to_utf[ch] : "#@#";
    }
    std::string get_itm_word(uint64 word) {
        std::string ret;
        for (uint i = 40; i <= 40; i-=8)
            ret += get_itm_char(word >> i);
        return ret;
    }

    std::string get_text_char (unsigned char ch) {
        return text_to_utf[ch & 63];
    }
    std::string get_text_word(uint64 word) {
        std::string ret;
        for (uint i = 42; i <= 42; i-=6)
            ret += get_text_char(word >> i);
        return ret;
    }

    std::string get_bytes(uint64 word) {
        std::string ret;
        for (uint i = 40; i <= 40; i-=8)
            ret += strprintf("%03o ", int(word >> i) & 0377);
        return ret;
    }

    // ISO decoders: the DMS variant is the simple one; the Pascal-B variant
    // renders control codes as _NNN. They are kept separate so DMS output is
    // unchanged.
    std::string dms_get_iso_char (unsigned char ch) {
        ch &= 0177;
        if (ch < 0140) { std::string ret; return ret = ch; }
        return std::string(&"ЮАБЦДЕФГХИЙКЛМНОПЯРСТУЖВЬЫЗШЭЩЧ\177"[2*(ch-0140)], 2);
    }
    std::string dms_get_iso_word(uint64 word) {
        std::string ret;
        for (uint i = 40; i <= 40; i-=8)
            ret += dms_get_iso_char(word >> i);
        return ret;
    }
    std::string get_iso_char (unsigned char ch) {
        ch &= 0177;
        if (ch >= 040 && ch < 0140) { std::string ret; return ret = ch; }
        if (ch < 040 || ch >= 0177) return strprintf("_%03o", ch);
        return std::string(&"ЮАБЦДЕФГХИЙКЛМНОПЯРСТУЖВЬЫЗШЭЩЧ\177"[2*(ch-0140)], 2);
    }
    std::string get_iso_word(uint64 word) {
        std::string ret;
        if (word < 256) return get_iso_char(word);
        for (uint i = 40; i <= 40; i-=8)
            ret += get_iso_char(word >> i);
        return ret;
    }

    // Likelihood tests.
    bool dms_is_iso (uint64 word) {
        for (uint i = 0; i < 48; i += 8) {
            uint val = (word >> i) & 0377;
            if (val < 040 || val >= 0177)
                return false;
        }
        return true;
    }
    bool is_likely_iso (uint64 word) {
        int zeromask = 0;
        for (uint i = 0; i < 48; i += 8) {
            uint val = (word >> i) & 0377;
            if (val != 0 && (val < 040 || val >= 0177))
                return false;
            if (val == 0) zeromask |= 1 << (i/8);
        }
        return zeromask != 63 &&
            (zeromask == 0 || ((zeromask+1) & zeromask) == 0 || zeromask == 62);
    }
    bool is_valid_iso (uint64 word) { return is_likely_iso(word); }

    bool is_likely_text(uint64 word) {
        // Of all groups of 6 bits, they should be left- or right-aligned,
        // there must be no spaces in the middle, and no unused codes/Cyrillics.
        int seen0 = 0, seen_char = 0;
        bool last0 = false;
        for (uint i = 42; i <= 42; i-=6) {
            uint val = (word >> i) & 077;
            switch (val) {
            case 001 ... 011:
            case 013 ... 016:
            case 032 ... 034:
            case 037:
            case 040:
            case 073 ... 077:
                return false;
            case 0:
                if (seen_char && seen0 && !last0) return false;
                ++seen0;
                last0 = true;
                break;
            default:
                if (seen_char && seen0 && last0) return false;
                ++seen_char;
                last0 = false;
            }
        }
        return seen_char > 1;
    }

    // Pascal-A GOST test (with optional -G short-circuit).
    bool pa_is_gost (uint32 addr, uint64 word) {
        if (gostoff.count(addr))
            return true;
        if (0 == (word >> 40)) return false;
        for (uint i = 0; i < 48; i += 8) {
            uint val = (word >> i) & 0377;
            if (val >= 0140) switch (val) {
                case 0377: case 0143: case 0162: case 0172:
                case 0175:
                    break;
                default:
                    return false;
                }
        }
        return true;
    }

    // Pascal-B scored GOST/ITM tests.
    bool is_valid_gost (uint64 word) {
        for (uint i = 0; i < 48; i += 8) {
            uint val = (word >> i) & 0377;
            switch (val) {
                case 0377: case 0143: case 0162: case 0172:
                case 0146:
                case 0175: case 0214:
                    break;
                case 020: case 021: case 024: case 034: /* unlikely chars */
                    return false;
                default:
                    if (val >= 0116)
                        return false;
                }
        }
        return true;
    }
    bool is_likely_gost (uint64 word) {
        int total = 0, zeros = 0;
        for (uint i = 0; i < 48; i += 8) {
            uint val = (word >> i) & 0377;
            if (total && val == 0) ++zeros, ++total;
            if (val) ++total;
            switch (val) {
                case 0377: case 0143: case 0162: case 0172:
                case 0146:
                case 0175: case 0214:
                    break;
                case 020: case 021: case 024: case 034:
                    return false;
                default:
                    if (val >= 0116)
                        return false;
                }
        }
        return (word >> 40) || ((word >> 32) && total-zeros > 2);
    }
    bool is_valid_itm(uint64 word) {
        for (uint i = 0; i < 48; i += 8) {
            uint val = (word >> i) & 0377;
            if (!itm_to_utf[val])
                return false;
        }
        return true;
    }
    bool is_likely_itm(uint64 word) {
        int total = 0, zeros = 0;
        for (uint i = 0; i < 48; i += 8) {
            uint val = (word >> i) & 0377;
            if (total && val == 0) ++zeros, ++total;
            if (val) ++total;
            if (!itm_to_utf[val])
                return false;
        }
        return (word >> 40) || ((word >> 32) && total-zeros > 2);
    }

    // ==================================================================
    // DMS backend (object files)                  [dtran2.cc / dtran3.cc]
    // ==================================================================
    void fill_lengths_dms() {
        // Detect packed or unpacked header.
        head_off = 0;
        if ((memory[1] >> 45) != 0) {
            // Skip entry table.
            while ((memory[head_off + 1] >> 45) != 0)
                head_off += 2;
            head_len  = memory[head_off];
            sym_len   = memory[head_off + 1];
            // Unknown: memory[head_off + 2]
            debug_len = memory[head_off + 3];
            long_len  = memory[head_off + 4];
            cmd_len   = memory[head_off + 5];
            bss_len   = memory[head_off + 6];
            const_len = memory[head_off + 7];
            data_len  = memory[head_off + 8];
            set_len   = memory[head_off + 9];
            cmd_off = head_off + 10;
        } else {
            head_len = memory[0] & 07777;
            sym_len = (memory[0] >> 12) & 07777;
            debug_len = (memory[0] >> 36);
            set_len = memory[1] & 077777;
            data_len = (memory[1] >> 15) & 077777;
            long_len = (memory[1] >> 30) & 077777;
            cmd_len = memory[2] & 077777;
            bss_len = (memory[2] >> 15) & 077777;
            const_len = (memory[2] >> 30) & 077777;
            cmd_off = 3;
        }
        if (bss_len != 0)
            fprintf(stderr, "BSS section not fully supported yet\n");
        table_off = cmd_off + cmd_len + const_len + data_len + set_len;
        long_off = table_off + head_len + sym_len;
        debug_off = long_off + long_len;
        comment_off = debug_off + debug_len;
        printf(" %s:,NAME, NEW DTRAN\n",
               get_text_word(memory[table_off-0]).c_str());
        printf("C Commands: %o\n", cmd_len);
        printf("C Constants: %o\n", const_len);
        printf("C BSS: %o\n", bss_len);
        printf("C Memory size: %o\n", cmd_len+const_len+bss_len);
        printf("C Header: %o\n", head_len);
        printf("C Symbols: %o\n", sym_len);
        printf("C Long symbols: %o\n", long_len);
        printf("C Debug: %o\n", debug_len);
        printf("C Data: %o + set: %o\n", data_len, set_len);
        printf("C Actual length of the object file is %o\n", comment_off);
        if (nolabels)
            printf(" /:,BSS,\n");
    }

    std::string gak(uint i) {
        std::string ret("G");
        ret += 'A'+i/128;
        ret += 'K'+i/8%16;
        ret += '/';
        ret += '0'+i%8;
        return ret;
    }

    void dump_sym(uint i, uint offset) {
        uint64 word = memory[i+offset];
        if ((word >> 15) == 0400) {
            uint val = word & 077777;
            if (val > 0100000-100)
                symtab[i] = strprintf("-%d", 0100000-val);
            else
                symtab[i] = strprintf(nooctal ? "%d" : "%oB", val);
        } else if ((word >> 15) == 0410) {
            uint off = word & 077777;
            symtab[i] = off < 010000 ?
                              strprintf("*%04oB", off)
                              : strprintf("*%05o", off);
            labels[off] = symtab[i];
        } else {
            if (word >> 42) {
                // The first char is not a space
                symtab[i] = get_text_word(word & 07777777700000000LL);
            } else if ((word & 00000400020000000LL) == 00000400020000000LL) {
                // Long ID
                uint loff = (word >> 24) & 03777;
                symtab[i] = get_text_word(memory[table_off + loff]);
            }
            if ((word & 057777777) == 043000000) {
                printf(" %s:,SUBP,\n", symtab[i].c_str());
            } else if ((word & 057000000) == 047000000) {
                printf(" %s:,LC,%d\n", symtab[i].c_str(), (uint)word & 0777777);
            } else if (((word >> 24) & 077774000) == 04000 &&
                       (word & 077777777) < 0100000) {
                uint sym = (word >> 24) & 03777;
                uint off = word & 077777;
                int ioff = off >= 040000 ? -(int)off : (int)off;
                if (noequs) {
                    symtab[i] = strprintf("%s%+d", symtab[sym].c_str(), ioff);
                } else {
                    symtab[i] = gak(i);
                    printf(" %s:,EQU,%s%+d\n", symtab[i].c_str(),
                           symtab[sym].c_str(), ioff);
                }
            } else {
                printf("C %5o: %016llo (%s) dunno\n", i, word, gak(i).c_str());
            }
        }
    }

    void dump_symtab() {
        printf("C Symbol table offset is %o\n", table_off);
        for (uint i = head_len; i < head_len + sym_len; ++i)
            dump_sym(i, table_off);
    }

    void mklabels_dms(uint32 memaddr, uint32 opcode, bool litconst) {
        int arg1 = (opcode & 07777) + (opcode & 0x040000 ? 070000 : 0);
        int arg2 = opcode & 077777;
        int struc = opcode & 02000000;
        unsigned reg = opcode >> 20;
        if (basereg && basereg == reg) {
            if (baseaddr == ~0u && (opcode & 03740000) == 02440000) {
                // Setting base reg by a relative address or a symbol
                if ((opcode & 074000) == 074000) {
                    unsigned sympos = opcode & 03777;
                    if (memory[table_off + sympos] >> 15 != 0410) {
                        fprintf(stderr, "@%05o Base register set to non-relative address\n", memaddr);
                        exit(1);
                    }
                    baseaddr = memory[table_off + sympos] & 077777;
                } else {
                    baseaddr = opcode & 037777;
                }
                baseop = opcode;
                fprintf(stderr, "@%05o Base register set to %05o\n", memaddr, baseaddr);
            } else if (struc) {
                if (baseaddr == ~0u || baseop != opcode) {
                    fprintf(stderr, "@%05o Base register used in a long-address insn\n", memaddr);
                    exit(1);
                }
            } else if (baseaddr == ~0u) {
                fprintf(stderr, "@%05o Base register used but not yet set\n", memaddr);
                exit(1);
            } else if (arg1 >= 04000) {
                fprintf(stderr, "@%05o Base register used with a symbol (%04o)\n", memaddr, arg1);
                exit(1);
            } else {
                unsigned off = baseaddr + arg1;
                if (off >= labels.size()) {
                    fprintf(stderr, "@%05o Base offset %05o too large\n", memaddr, arg1);
                    exit(1);
                }
                if (labels[off].empty())
                    labels[off] = litconst ? get_literal_dms(off) : strprintf("*%04oB", off);
            }
        } else if (struc && arg2 >= 074000) {
            // nothing
        } else if (struc && arg2 >= 040000 && (unsigned)(arg2 & 037777) < labels.size()) {
            unsigned off = arg2 & 037777;
            if (labels[off].empty())
                labels[off] = off < 010000 ?
                                    strprintf("*%04oB", off) :
                                    strprintf("*%05o", off);
        }
    }

    void prinsn_dms (uint32 memaddr, uint32 opcode) {
        (void)memaddr;
        unsigned reg = opcode >> 20;
        int arg1 = (opcode & 07777) + (opcode & 0x040000 ? 070000 : 0);
        int arg2 = opcode & 077777;
        int struc = opcode & 02000000;

        int i = get_opidx(opcode);
        opcode_e type = op[i].type;
        std::string opname = op[i].name;
        if (type == OPCODE_ILLEGAL) {
            opname = struc ? strprintf("%2o", (opcode >> 15) & 037)
                : strprintf("%03o", (opcode >> 12) & 0177);
        }
        std::string operand;
        if (struc && arg2 >= 074000) {
            operand = strprintf("%s", symtab[arg2 & 03777].c_str());
        } else if (struc && arg2 >= 040000 && (unsigned)(arg2 & 037777) < labels.size()) {
            unsigned off = arg2 & 037777;
            if (labels[off].empty())
                labels[off] = off < 010000 ?
                                    strprintf("*%04oB", off) :
                                    strprintf("*%05o", off);
            operand = labels[off];
        } else if (!struc && arg1 >= 04000 && arg1 < 010000) {
            operand = symtab[arg2 & 03777];
        } else if (!struc && arg1 >= 070000) {
            operand = "dunno";
        } else {
            if (unsigned val = struc ? arg2 : arg1) {
                if (type == OPCODE_IMM64)             // YTA/E+N/E-N/ASN: always 64+/-N when non-zero
                    operand = strprintf("64%+d", val-64);
                else if (type == OPCODE_REG1 || val < 8)
                    operand = strprintf("%d", val);
                else
                    operand = strprintf(nooctal ? "%d" : "%oB", val);
            }
        }

        if (basereg && reg == basereg) {
            if (op[i].opcode == 02400000) {
                opname = "BASE";
            } else {
                reg = 0;
                operand = labels[baseaddr + arg1];
            }
        }
        if (nolabels && operand[0] == '*' && !strchr(operand.c_str(), '+')) {
            char * end;
            int off = strtol(operand.c_str()+1, &end, 8);
            if (end - operand.c_str() > 4)
                operand = strprintf(nooctal ? "/+%d" : "/+%oB", off);
        }
        if (reg) printf("%d,", reg); else printf(",");
        printf("%s,%s\n", opname.c_str(), operand.c_str());
    }

    std::string get_literal_dms(uint32 addr) {
        uint64 val = memory[addr + cmd_off];
        std::string ret;
        if ((val >> 24) == 064000000) {
            unsigned d = val & 077777777;
            if (d > 10000)
                ret = strprintf("%08oB", d);
            else
                ret = strprintf("(%d)", d);
        } else if (dms_is_iso(val)) {
            ret = strprintf("iso('%s')", dms_get_iso_word(val).c_str());
        } else if (is_likely_text(val)) {
            ret = strprintf("%lloC(*\"%s\"*)", val, get_text_word(val).c_str());
        } else if (val >= 0101 && val < 96) {
            ret = strprintf("char('%c')", char(val));
        } else {
            ret = strprintf("(%lloC)", val);
        }
        return ret;
    }

    // Convert a string: quote apostrophe symbols.
    std::string quoteiso(std::string str) {
        std::string buf;
        for (const char &c : str) {
            if (c == '\'')
                buf += "\'47";
            buf += c;
        }
        return buf;
    }

    void prconst_dms (uint32 addr, uint32 len, bool litconst) {
        for (unsigned cur = addr; cur < addr + len; ++cur) {
            uint64 val = memory[cur + cmd_off];
            if (litconst) {
                printf(" /%d:", cur-cmd_len);
            } else if (labels[cur].empty()) {
                printf(" ");
            } else {
                printf(" %s:", labels[cur].c_str());
            }
            if ((val >> 24) == 064000000) {
                unsigned d = val & 077777777;
                if (d > 200000)
                    printf(",INT,%08oB\n", d);
                else
                    printf(",INT,%d\n", d);
            } else if (dms_is_iso(val)) {
                printf(",ISO, 6H%s . %s\n", quoteiso(dms_get_iso_word(val)).c_str(), get_bytes(val).c_str());
            } else if (is_likely_text(val)) {
                printf(",TEXT, 8H%s\n", get_text_word(val).c_str());
            } else {
                printf(",LOG,%llo\n", val);
            }
        }
    }

    void prsets() {
        for (unsigned cur = table_off - set_len; cur < table_off; ++cur) {
            uint64 word = memory[cur];
            unsigned len = word >> 36;
            unsigned from = (word >> 24) & 03777;
            unsigned cnt = (word >> 12) & 07777;
            unsigned to = word & 03777;
            std::string src = symtab[from];
            printf(" %d,SET,%s\n", len, src.c_str());
            printf(" %d,   ,%s\n", cnt, symtab[to].c_str());
        }
    }

    void prdata() {
        // Print data initializations as assignments when the source allows it.
        for (unsigned cur = table_off - set_len; cur < table_off; ++cur) {
            uint64 word = memory[cur];
            unsigned len = word >> 36;
            unsigned from = (word >> 24) & 03777;
            unsigned cnt = (word >> 12) & 07777;
            unsigned to = word & 03777;
            std::string src = symtab[from];
            unsigned off = ~0;

            if (src[0] == '*') {
                char * end;
                off = strtol(src.c_str()+1, &end, 8);
                do {
                    switch (*end) {
                    case '\0': break;
                    case '+':
                        if (isdigit(end[1]))
                            off += atoi(end+1);
                        else
                            off = ~0;
                        break;
                    case 'B':
                        ++end;
                        continue;
                    default:
                        off = ~0;
                        break;
                    }
                    break;
                } while (1);
            }
            if (off != ~0u) {
                for (unsigned tocnt = 0; tocnt < cnt; ++tocnt) {
                    for (unsigned fromcnt = 0; fromcnt < len; ++fromcnt) {
                        printf(" ,XTA,%s\n", get_literal_dms(off+fromcnt).c_str());
                        std::string temp;
                        size_t plus = symtab[to].find('+');
                        if (plus != std::string::npos && (tocnt || fromcnt)) {
                            unsigned was = atoi(symtab[to].c_str() + plus + 1);
                            temp = strprintf("%.*s+%d", (int)plus, symtab[to].c_str(),
                                             was + tocnt*len+fromcnt);
                        } else if (tocnt || fromcnt) {
                            temp = symtab[to] + strprintf("%+d", tocnt*len+fromcnt);
                        } else {
                            temp = symtab[to];
                        }
                        printf(" ,ATX,%s\n", temp.c_str());
                    }
                }
            } else {
                printf(" %d,SET,%s\n", len, src.c_str());
                printf(" %d,   ,%s\n", cnt, symtab[to].c_str());
            }
        }
    }

    void prbss (uint32 addr, uint32 limit) {
        // TODO: support BSS section
        printf("C here be BSS entries, %05o-%05o\n", addr, addr+limit-1);
    }

    void prtext_dms (bool litconst) {
        uint32 addr = 0;
        uint32 limit = cmd_len;
        for (uint32 cur = addr; cur < limit; ++cur) {
            uint64 & opcode = memory[cur + cmd_off];
            mklabels_dms(cur, opcode >> 24, litconst);
            mklabels_dms(cur, opcode & 0xffffff, litconst);
        }
        if (nolabels)
            puts(" /:,BSS,");
        for (; addr < limit; ++addr) {
            uint64 opcode;
            if (!labels[addr].empty()) {
                if (nolabels)
                    printf(" :");
                else
                    printf(" %s:", labels[addr].c_str());
            } else
                putchar(' ');
            opcode = memory[addr + cmd_off];
            prinsn_dms (addr, opcode >> 24);
            // Do not print the non-insn part of a word if it is a placeholder.
            opcode &= 0xffffff;
            if (opcode == 02200000) {
                opcode = memory[addr + cmd_off] >> 24;
                opcode &= 03700000;
                if (opcode != 03100000 && labels[addr+1].empty())
                    labels[addr+1] = " ";
            } else {
                putchar(' ');
                prinsn_dms (addr, opcode);
            }
        }
    }

    // ==================================================================
    // Pascal shared reachability                            [dtran4.cc]
    // ==================================================================
    uint check_chain(uint prev) {
        uint prevop = (prev & 03700000) >> 15;
        if (prevop == 024 && (prev >> 20) == 13) {
            uint next_addr = prev & 077777;
            if (next_addr && next_addr < total_len) {
                mklabel(next_addr);
                return next_addr;
            }
        }
        return 0;
    }

    uint find_code_offset() {
        uint min_addr = total_len;
        std::vector<uint> todo;
        todo.push_back(main_off);
        if (!codeoff.empty()) {
            for (int off : codeoff) {
                todo.push_back(off);
                mklabel(off);
            }
            fprintf(stderr, "Got %lu known entry points\n", codeoff.size());
        }

        // Find all VJM targets by transitive closure, ignoring indirect calls.
        while (!todo.empty()) {
            uint cur = todo.back();
            todo.pop_back();
            if (code_map[cur]) continue;
            if (cur >= total_len) continue;

            code_map[cur] = true;
            if (cur < min_addr) min_addr = cur;
            uint64 word = memory[cur];
            uint insn[2];
            insn[0] = word >> 24;
            insn[1] = word & 0xFFFFFF;
            for (uint i = 0; i < 2; ++i) {
                uint cinsn = insn[i];
                uint next_addr;
                uint opcode = (cinsn & 03700000) >> 15;
                if (opcode == 031 || opcode == 034 || opcode == 035) { // VJM/VZM/V1M, any reg
                    next_addr = cinsn & 077777;
                    if (next_addr && next_addr < total_len) {
                        mklabel(next_addr);
                        todo.push_back(next_addr);
                    }
                    todo.push_back(cur+1);
                    // (value _IN type) is 13,VTM,iffalse; 14,VJM,test
                    if (opcode == 031 && (cinsn >> 20) == 14) {
                        uint prev = i == 1 ? insn[0] : memory[cur-1] & 0xFFFFFF;
                        if (uint next = check_chain(prev))
                            todo.push_back(next);
                    }
                    break;
                } else if (opcode == 030) { // UJ
                    next_addr = cinsn & 077777;
                    uint idx = cinsn >> 20;
                    if (next_addr && next_addr < total_len && idx == 0) {
                        todo.push_back(next_addr);
                        mklabel(next_addr);
                        uint prev = i == 1 ? insn[0] : memory[cur-1] & 0xFFFFFF;
                        if (uint next = check_chain(prev))
                            todo.push_back(next);
                    } else if ((next_addr == cur || next_addr == cur+1) && (cinsn >> 20) != 0) {
                        // This looks like a jump table.
                        for (uint t = next_addr; t < total_len; ++t) {
                            uint64 entry = memory[t];
                            uint entop = (entry >> (24+15)) & 037;
                            uint entidx = entry >> (24+20);
                            if (entop == 030 && entidx == 0) // A jump
                                todo.push_back(t);
                            else
                                break;
                        }
                    }
                    break;
                } else if ((cinsn & 077600000) == 002600000   // U1A, UZA, 0 reg
                           || ((cinsn & 077700000) == 042600000)) { // 8,UZA - case stmt
                    next_addr = cinsn & 077777;
                    if (next_addr && next_addr < total_len) {
                        todo.push_back(next_addr);
                        mklabel(next_addr);
                    }
                    if (i == 1) todo.push_back(cur + 1);
                } else if (i == 1)
                    todo.push_back(cur + 1);
            }
        }
        return forced_code_off ? forced_code_off : min_addr;
    }

    // ==================================================================
    // Pascal-A backend                                      [dtran1.cc]
    // ==================================================================
    void fill_lengths_pa() {
        head_len = 0;           // the binary is read to address 0
        total_len = memory[5] & 077777;
        main_off = memory[010] & 077777;
        mklabel(main_off);
        std::fill(code_map, code_map+32768, false);
        code_off = find_code_offset();
        printf(" %s:,NAME, NEW DTRAN\n", get_gost_word(memory[1]).c_str());
        printf("C Memory size: %o\n", total_len);
        printf("C Code start: %o\n", code_off);
        printf("C Program start: %o\n", main_off);
        if (nolabels)
            printf(" /:,BSS,\n");
    }

    std::string get_literal_pa(uint32 addr) {
        uint64 val = memory[addr];
        std::string ret;
        uint64 d = val & 0x7FFFFFFFFFFFull;
        if ((val >> 38) == 01000 && d != 0) {
            if (d > 10000)
                ret = strprintf("DIV%d", (1ull << 40)/(d-1));
            else
                ret = strprintf("mul(%d)", (int)d);
        } else if (pa_is_gost(addr, val)) {
            ret = strprintf("'%s'", get_gost_word(val).c_str());
        } else if (val >= 100 && val <= 10000) {
            ret = strprintf("(%d)", (int)val);
        } else {
            ret = strprintf("(%lloC)", val);
        }
        return ret;
    }

    void mklabels_pa(uint32 memaddr, uint32 opcode, bool litconst) {
        (void)memaddr;
        int arg1 = (opcode & 07777) + (opcode & 0x040000 ? 070000 : 0);
        int arg2 = opcode & 077777;
        int struc = opcode & 02000000;
        uint reg = opcode >> 20;
        uint op = struc ? (opcode >> 15) & 037 : (opcode >> 12) & 077;
        if (!struc && basereg && reg == basereg)
            reg = 0;
        if (!struc && !reg && arg1 >= 011 && (uint)arg1 < total_len && labels[arg1].empty()) {
            labels[arg1] = (litconst && (uint)arg1 < code_off) ? get_literal_pa(arg1) :
                strprintf("/%d", arg1);
        } else if (struc && arg2 >= 011 && (uint)arg2 < total_len &&
                   !(((op == 023) || (op == 025)) && reg != 0)) {
            if (labels[arg2].empty() && code_map[arg2])
                mklabel(arg2);
        }
    }

    void prinsn_pa (uint32 memaddr, uint32 opcode) {
        (void)memaddr;
        uint reg = opcode >> 20;
        int arg1 = (opcode & 07777) + (opcode & 0x040000 ? 070000 : 0);
        int arg2 = opcode & 077777;
        int struc = opcode & 02000000;
        int arg = struc ? arg2 : arg1;
        static bool prev_addrmod = false;

        int i = get_opidx(opcode);
        opcode_e type = op[i].type;
        std::string opname = op[i].name;
        if (type == OPCODE_ILLEGAL) {
            opname = struc ? strprintf("%2o", (opcode >> 15) & 037)
                : strprintf("%03o", (opcode >> 12) & 0177);
        } else if (!struc && basereg && reg == basereg) {
            reg = 0;
        }
        std::string operand;
        if (arg)
            operand = strprintf(struc ? "U%05o" : "U%04o", struc ? arg2 : arg1);
        if (struc && arg2 >= 074000) {
            std::string & sym = symtab[arg2 & 03777];
            if (sym.empty()) {
                if (type == OPCODE_REG2 || (type == OPCODE_BRANCH && reg != 0))
                    operand = strprintf("%d", arg2 - 0100000);
                else
                    operand = strprintf("P/%04o", arg2 & 03777);
            } else
                operand = symtab[arg2 & 03777];
        } else if (struc && arg2 && (size_t)arg2 < labels.size()) {
            uint off = arg2;
            if (labels[off].empty() || type == OPCODE_REG2 || (reg != 0 && type == OPCODE_ADDRMOD))
                operand = strprintf("%d", off);
            else
                operand = labels[off];
        } else if (!struc && arg1 >= 074000) {
            operand = strprintf("C/%04o", arg1 & 03777);
        } else if (uint val = struc ? arg2 : arg1) {
            if (type == OPCODE_IMM64)                 // YTA/E+N/E-N/ASN: always 64+/-N when non-zero
                operand = strprintf("64%+d", val-64);
            else if (type == OPCODE_REG1 || val < 8 || prev_addrmod)
                operand = strprintf("%d", val);
            else if (!struc && !reg && type != OPCODE_IMMEX &&
                     arg1 >= 011 && (uint)arg1 < code_off)
                operand = labels[arg1];
            else
                operand = strprintf(type != OPCODE_IMMEX && nooctal ? "%d" : "%oB", val);
        }

        if (nolabels && operand[0] == 'L' && !strchr(operand.c_str(), '+')) {
            char * end;
            int off = strtol(operand.c_str()+1, &end, 8);
            if (end - operand.c_str() > 4)
                operand = strprintf(nooctal ? "/+%d" : "/+%oB", off);
        }
        if (reg) printf("%d,", reg); else printf(",");
        printf("%s,%s\n", opname.c_str(), operand.c_str());
        prev_addrmod = type == OPCODE_ADDRMOD;
    }

    void pr1const_pa(uint cur, bool litconst) {
        uint64 val = memory[cur];
        if (litconst && !nodlabels) {
            printf(" /%d:", cur);
        } else if (labels[cur].empty()) {
            printf(" ");
        } else {
            printf(" %s:", labels[cur].c_str());
        }
        if (val < 65536) {
            printf(",INT,%d . 0%o\n", (int)val, (int)val);
        } else if (pa_is_gost(cur, val)) {
            printf(",GOST, |%s| %s\n", get_gost_word(val).c_str(), get_bytes(val).c_str());
        } else {
            printf(",LOG,%llo\n", val);
        }
    }

    void prconst_pa (bool litconst) {
        for (uint cur = 011; cur < code_off; ++cur)
            pr1const_pa(cur, litconst);
    }

    void prtext_pa (bool litconst) {
        uint32 addr = code_off;
        uint32 limit = total_len;
        for (uint32 cur = addr; cur < limit; ++cur) {
            if (code_map[cur]) {
                uint64 & opcode = memory[cur];
                mklabels_pa(cur, opcode >> 24, litconst);
                mklabels_pa(cur, opcode & 0xffffff, litconst);
            }
        }
        if (nolabels)
            puts(" /:,BSS,");
        for (; addr < limit; ++addr) {
            if (!code_map[addr]) { pr1const_pa(addr, litconst); continue; }
            uint64 opcode;
            if (!labels[addr].empty()) {
                if (nolabels)
                    printf(" :");
                else
                    printf(" %s:", labels[addr].c_str());
            } else
                putchar(' ');
            opcode = memory[addr];
            prinsn_pa (addr, opcode >> 24);
            bool addrmod = ((opcode >> 24) & 03600000) == 02200000;
            opcode &= 0xffffff;
            if ((opcode == 0 && !addrmod) || opcode == 02200000) {
                opcode = memory[addr] >> 24;
                opcode &= 03700000;
                if (opcode != 03100000 && labels[addr+1].empty())
                    labels[addr+1] = " ";
            } else {
                putchar(' ');
                prinsn_pa (addr, opcode);
            }
        }
    }

    uint label_pattern(void * pattern, size_t size, std::string name) {
        uint64* where = (uint64*)memmem(memory, sizeof(memory[0])*total_len, pattern, size);
        if (where) labels[where-memory] = name;
        return where ? where-memory : 0100000;
    }

    void label_patterns_pa() {
        uint64 p2[] = {
            00037000300420013LL, 07444001374440002LL,
            00043001600430001LL, 05400000263000000LL
        };
        uint64 p3[] = {
            00037000300420013LL, 07444001374440003LL,
            00043001600430002LL, 05400000263000000LL
        };
        uint64 p4[] = {
            00037000300420013LL, 07444001374440004LL,
            00043001600430003LL, 05400000263000000LL
        };
        uint64 p32[] = { 05444000314100002LL, 00040000273000000LL };
        uint64 p43[] = { 05444000420100002LL, 00040000373000000LL };
        label_pattern(p32, sizeof(p32), "P/32");
        label_pattern(p43, sizeof(p43), "P/43");
        label_pattern(p2, sizeof(p2), "P/2");
        label_pattern(p3, sizeof(p3), "P/3");
        label_pattern(p4, sizeof(p4), "P/4");
        labels[012670] = "P/4";
        symtab[012] = "P/E";
        symtab[031] = "P/WOLN";
        symtab[034] = "P/MD";
        symtab[047] = "P/7A";
        symtab[055] = "P/RC";
        symtab[0100] = "P/A7";
        symtab[0101] = "P/WC";
        symtab[0102] = "P/6A";
        symtab[0104] = "P/WI";
        symtab[0110] = "P/IN";
        symtab[0111] = "P/SS";
        symtab[0057] = "P/TR"; // trunc
        symtab[0141] = "P/TR"; // trunc, same as 0057
        symtab[0136] = "P/0060"; // signed mult. correction
        symtab[0222] = "P/0026"; // get(input)
        symtab[0257] = "P/0030"; // put(output)
        symtab[0313] = "P/0032"; // rewrite(output)
        symtab[0337] = "P/0033"; // unpck
        symtab[0173] = "P/0023"; // pck
        symtab[0715] = "P/0040"; // put
        symtab[0760] = "P/0041"; // get
        symtab[0202] = "P/0024"; // intToReal, a no-op in P-M
        symtab[01675] = "P/0066"; // bind
    }

    // ==================================================================
    // Pascal-B backend                                      [dtran4.cc]
    // ==================================================================
    void fill_lengths_pb() {
        head_len = 0;           // the binary is read to address 0
        total_len = ((memory[02011] && memory[02011] < 037) ?
            memory[02011] : memory[02010]) * 02000 + 02000;
        main_off = 02000;
        mklabel(main_off);
        std::fill(code_map, code_map+32768, false);
        code_off = std::max(main_off, find_code_offset());
        // Pascal-B has no name/date word in the file; emit a pro-forma name.
        printf(" PASCODER:,NAME, NEW DTRAN\n");
        printf("C Memory size: %o\n", total_len);
        printf("C Code start: %o\n", code_off);
        printf("C Program start: %o\n", main_off);
        printf("C Aligning line numbers\nC to addresses\nC of literal constants\n");
        if (nolabels)
            printf(" /:,BSS,\n");
    }

    std::string get_literal_pb(uint32 addr) {
        uint64 val = memory[addr];
        std::string ret;
        uint64 d = val & 0x7FFFFFFFFFFFull;
        if ((val >> 38) == 01000 && d != 0) {
            if (d > 10000)
                ret = strprintf("DIV%d", (1ull << 40)/(d-1));
            else
                ret = strprintf("mul(%d)", (int)d);
        } else if (format_map[addr] == fGOST) {
            ret = strprintf("'%s'", get_gost_word(val).c_str());
        } else if (format_map[addr] == fISO) {
            ret = strprintf("'%s'", get_iso_word(val).c_str());
        } else if (format_map[addr] == fITM) {
            ret = strprintf("itm'%s'", get_itm_word(val).c_str());
        } else if (format_map[addr] == fTEXT) {
            ret = strprintf("\"%s\"", get_text_word(val).c_str());
        } else if (format_map[addr] == fINT) {
            int dd = val;
            ret = strprintf("(%d)", dd);
        } else if (textoff.count(addr)) {
            ret = strprintf("|%s|", get_text_word(val).c_str());
        } else {
            ret = strprintf("(%lloC)", val);
        }
        return ret;
    }

    void mklabels_pb(uint32 memaddr, uint32 opcode, bool litconst) {
        int arg1 = (opcode & 07777) + (opcode & 0x040000 ? 070000 : 0);
        int arg2 = opcode & 077777;
        int struc = opcode & 02000000;
        uint reg = opcode >> 20;
        uint op = struc ? (opcode >> 15) & 037 : (opcode >> 12) & 077;
        if (basereg && basereg == reg) {
            if (baseaddr == ~0u && (opcode & 03700000) == 02400000) {
                baseaddr = opcode & 037777;
                baseop = opcode;
                fprintf(stderr, "@%05o Base register set to %05o\n", memaddr, baseaddr);
            } else if (struc) {
                if (memaddr < code_len && (baseaddr == ~0u || baseop != opcode))
                    fprintf(stderr, "@%05o Base register used in a long-address insn\n", memaddr);
            } else if (baseaddr == ~0u) {
                fprintf(stderr, "@%05o Base register used but not yet set\n", memaddr);
            } else if (memaddr < code_len && arg1 >= 010000) {
                fprintf(stderr, "@%05o Base register used with a negative value (%04o)\n", memaddr, arg1);
            } else {
                uint off = (baseaddr + arg1) % 32768;
                if (off >= labels.size())
                    fprintf(stderr, "@%05o Base offset %05o too large\n", memaddr, arg1);
                if (labels[off].empty())
                    labels[off] = litconst ? get_literal_pb(off) : strprintf("L%04o", off);
            }
        } else if (!struc && !reg && arg1 >= 011 && (uint)arg1 < total_len && labels[arg1].empty()) {
            labels[arg1] = litconst ? get_literal_pb(arg1) : strprintf("L%04o", arg1);
        } else if (struc && arg2 >= 011 && (uint)arg2 < total_len &&
                   (op == 030 || op == 037 || op == 024) && reg != 10) {
            if (labels[arg2].empty() && (code_map[arg2] || reg == 11)) {
                if (litconst && op == 024 && reg == 11)
                    /* labels[arg2] = get_literal_pb(arg2) */;
                else
                    mklabel(arg2);
            }
        }
    }

    void prinsn_pb (uint32 memaddr, uint32 opcode) {
        (void)memaddr;
        uint reg = opcode >> 20;
        int arg1 = (opcode & 07777) + (opcode & 0x040000 ? 070000 : 0);
        int arg2 = opcode & 077777;
        int struc = opcode & 02000000;
        int arg = struc ? arg2 : arg1;
        static bool prev_addrmod = false;

        int i = get_opidx(opcode);
        opcode_e type = op[i].type;
        std::string opname = op[i].name;
        if (type == OPCODE_ILLEGAL) {
            opname = struc ? strprintf("%2o", (opcode >> 15) & 037)
                : strprintf("%03o", (opcode >> 12) & 0177);
        } else if (!struc && basereg && reg == basereg) {
            reg = 0;
            if (baseaddr != ~0u) arg1 += baseaddr;
        }
        std::string operand;
        if (arg)
            operand = strprintf(struc ? "U%05o" : "U%04o", struc ? arg2 : arg1);
        if (struc && arg2 && (size_t)arg2 < labels.size()) {
            uint off = arg2;
            bool good = code_off <= off && off <= total_len;
            if (!good || labels[off].empty() || ((opcode >> 15) & 037) == 025 || (reg != 0 && type == OPCODE_ADDRMOD))
                operand = strprintf("%d", off);
            else
                operand = labels[off];
        } else if (uint val = struc ? arg2 : arg1) {
            if (type == OPCODE_IMM64)                 // YTA/E+N/E-N/ASN: always 64+/-N when non-zero
                operand = strprintf("64%+d", val-64);
            else if (type == OPCODE_REG1 || val < 8 || prev_addrmod)
                operand = strprintf("%d", val);
            else if (!struc && !reg && type != OPCODE_IMMEX &&
                     (uint)arg1 >= code_off && (uint)arg1 < code_len)
                operand = labels[arg1];
            else
                operand = strprintf(type != OPCODE_IMMEX && nooctal ? "%d" : "%oB", val);
        }

        if (basereg && reg == basereg) {
            if (op[i].opcode == 02400000) {
                opname = "BASE";
            } else {
                reg = 0;
                operand = labels[(baseaddr + arg1) % 32768];
            }
        }

        if (nolabels && operand[0] == 'L' && !strchr(operand.c_str(), '+')) {
            char * end;
            int off = strtol(operand.c_str()+1, &end, 8);
            if (end - operand.c_str() > 4)
                operand = strprintf(nooctal ? "/+%d" : "/+%oB", off);
        }
        if (reg) printf("%d,", reg); else printf(",");
        printf("%s,%s\n", opname.c_str(), operand.c_str());
        prev_addrmod = type == OPCODE_ADDRMOD;
    }

    void pr1const_pb(uint cur, bool litconst) {
        (void)litconst;
        uint64 val = memory[cur];
        if (!nodlabels) {
            printf(" /%d:", cur);
        } else if (labels[cur].empty() ||
                   (labels[cur][0] != 'L' && labels[cur][0] != '/')) {
            printf(" ");
        } else {
            printf(" %s:", labels[cur].c_str());
        }

        if (gostoff.count(cur)) {
            printf(",GOST, |%s| %s\n", get_gost_word(val).c_str(), get_bytes(val).c_str());
            return;
        }
        if (itmoff.count(cur)) {
            printf(",ITM, |%s| %s\n", get_itm_word(val).c_str(), get_bytes(val).c_str());
            return;
        }
        if (isooff.count(cur)) {
            printf(",ISO, |%s| %s\n", get_iso_word(val).c_str(), get_bytes(val).c_str());
            return;
        }
        if (textoff.count(cur)) {
            printf(",TEXT, |%s| %s\n", get_text_word(val).c_str(), get_bytes(val).c_str());
            return;
        }
        switch (format_map[cur]) {
        case fINT: printf(",INT,%d . 0%o\n", (int)val, (int)val); break;
        case fGOST: printf(",GOST, |%s| %s\n", get_gost_word(val).c_str(), get_bytes(val).c_str()); break;
        case fISO: printf(",ISO, |%s| %s\n", get_iso_word(val).c_str(), get_bytes(val).c_str()); break;
        case fTEXT: printf(",TEXT, |%s| %s\n", get_text_word(val).c_str(), get_bytes(val).c_str()); break;
        case fITM: printf(",ITM, |%s| %s\n", get_itm_word(val).c_str(), get_bytes(val).c_str()); break;
        case fLOG: printf(",LOG,%llo\n", val);
        }
    }

    void populate_formats() {
        for (uint cur = 011; cur < total_len; ++cur) {
            if (code_map[cur])
                continue;
            if (gostoff.count(cur) && isooff.count(cur)) {
                fprintf(stderr, "Make up your mind regarding offset %d (%#o)\n", cur, cur);
            } else if (gostoff.count(cur)) {
                format_map[cur] = fGOST;
            } else if (itmoff.count(cur)) {
                format_map[cur] = fITM;
            } else if (isooff.count(cur)) {
                format_map[cur] = fISO;
            } else {
                uint64 val = memory[cur];
                int gost_score = 0;
                int itm_score = 0;
                int iso_score = isooff.count(-cur) ? 0 : is_valid_iso(val);
                if (gost_score)
                    gost_score += 2*is_likely_gost(val) +
                        is_likely_gost(memory[cur-1]) +
                        is_likely_gost(memory[cur+1]);
                if (itm_score)
                    itm_score += 2*is_likely_itm(val) +
                        is_likely_itm(memory[cur-1]) +
                        is_likely_itm(memory[cur+1]);
                if (iso_score)
                    iso_score += 2*is_likely_iso(val) +
                        is_likely_iso(memory[cur-1]) +
                        is_likely_iso(memory[cur+1]);

                if ((val >> 24) == 064000000 || (val >> 24) == 064377777) {
                    format_map[cur] = fINT;
                } else if (gost_score && gost_score > iso_score) {
                    format_map[cur] = fGOST;
                } else if (iso_score && iso_score >= gost_score) {
                    format_map[cur] = fISO;
                } else if (is_likely_text(memory[cur])) {
                    format_map[cur] = fTEXT;
                } else {
                    format_map[cur] = is_likely_iso(memory[cur]) ? fISO :
                        itm_score && val <= 0xffffff ? fITM : fLOG;
                }
            }
        }
    }

    void prtext_pb (bool litconst) {
        uint32 addr = code_off;
        uint32 limit = total_len;
        populate_formats();
        for (uint32 cur = addr; cur < limit; ++cur) {
            if (code_map[cur]) {
                uint64 & opcode = memory[cur];
                mklabels_pb(cur, opcode >> 24, litconst);
                mklabels_pb(cur, opcode & 0xffffff, litconst);
            }
        }
        if (nolabels)
            puts(" /:,BSS,");
        for (; addr < limit; ++addr) {
            if (addr % 64 == 0)
                printf("C ---------- %05o ----------\n", addr);
            if (!code_map[addr] || isooff.count(addr) || gostoff.count(addr)) {
                pr1const_pb(addr, litconst);
                continue;
            }
            uint64 opcode;
            if (!labels[addr].empty() &&
                (labels[addr][0] == 'L' || labels[addr][0] == '/')) {
                if (nolabels)
                    printf(" :");
                else
                    printf(" %s:", labels[addr].c_str());
            } else
                putchar(' ');
            opcode = memory[addr];
            prinsn_pb (addr, opcode >> 24);
            bool addrmod = ((opcode >> 24) & 03600000) == 02200000;
            opcode &= 0xffffff;
            if ((opcode == 0 && !addrmod) || opcode == 02200000) {
                opcode = memory[addr] >> 24;
                opcode &= 03700000;
                if (opcode != 03100000 && labels[addr+1].empty())
                    labels[addr+1] = " ";
            } else {
                putchar(' ');
                prinsn_pb (addr, opcode);
            }
        }
    }

    struct opfields { uint reg; bool struc; int op; };
    opfields decode(uint opcode) {
        opfields ret;
        ret.reg = opcode >> 20;
        ret.struc = opcode & 02000000;
        ret.op = (opcode >> 15) & 037;
        return ret;
    }

    void prsyms(uint cur, uint start) {
        uint64 name = memory[start];
        uint line = memory[start+1];
        static const char * typestr[] = {
            "real", "int", "char", "scalar", "array", "other", "file", "type 7"
        };
        printf("Routine %s @%05o, line %d:\n", get_text_word(name).c_str(), cur, line);
        typedef std::map<int, std::pair<uint64, uint64> > syms_t;
        syms_t syms;
        for (uint addr = start+2; memory[addr]; addr += 2)
            syms[memory[addr+1] & 077777] = std::make_pair(memory[addr], memory[addr+1]);
        for (syms_t::iterator it = syms.begin(); it != syms.end(); ++it) {
            uint64 flags = it->second.second;
            int type = (flags >> 15) & 7;
            int size = (flags >> 33);
            int offset = flags & 077777;
            printf("%s size %5o offset %05o - %s\n",
                   get_text_word(it->second.first).c_str(), size, offset, typestr[type]);
        }
        printf("\tthat is all\n");
    }

    // Pascal-monitor symbol table: each routine starts with
    // "16 24 base 16 25 offset". This is unique enough.
    void prsymtab() {
        uint32 addr = code_off;
        uint32 limit = total_len;
        uint prev_insn = 0;
        bool right = false;
        for (uint32 cur = addr; cur < limit; cur += !right) {
            uint insn = right ? memory[cur] & 0xffffff : memory[cur] >> 24;
            if (decode(insn).reg == 016 && decode(insn).struc &&
                decode(insn).op == 025 && decode(prev_insn).reg == 016 &&
                decode(prev_insn).struc && decode(prev_insn).op == 024) {
                uint start = (insn & 077777) + (prev_insn & 077777);
                if (start >= addr && start < limit)
                    prsyms(cur, start);
            }
            prev_insn = insn;
            right = !right;
        }
    }

    void label_patterns_pb() {
        /* Register saving subroutine level N is 4 distinctive words. */
        uint64 psav[] = {
            00037000300420007LL, 07444000774440000LL,
            00043001500430000LL, 03400000273000000LL
        };
        uint min_pattern = 077777;
        for (int i = 2; i <= 6; ++i) {
            psav[1] = (psav[1] & ~7LL) | i;
            psav[2] = (psav[2] & ~7LL) | (i-1);
            std::string name = std::string("P/") + char('0'+i);
            uint addr = label_pattern(psav, sizeof(psav), name);
            if (addr != 0100000)
                fprintf(stderr, "Address of %s is %05o\n", name.c_str(), addr);
            if (addr < min_pattern) min_pattern = addr;
        }
        /* Register restoring subroutine from M to N, 2 <= M < N <= 6. */
        uint64 pret[5];
        uint64 first = 05444000000100002LL; // 11,MTJ,N   N,XTA,2
        uint64 mid =   00040000000100002LL; //   ,ATI,K   K,XTA,2
        uint64 last =  00040000073000000LL; //   ,ATI,M  14, UJ,
        for (uint64 m = 2; m < 6; ++m)
            for (uint64 n = m+1; n <= 6; ++n) {
                int p = 0;
                pret[p++] = first | (n << 24) | (n<<20);
                for (uint64 k = n-1; k > m; --k)
                    pret[p++] = mid | (k << 24) | (k<<20);
                pret[p++] = last | (m<<24);
                uint addr = label_pattern(pret, p*sizeof(uint64),
                                          (std::string("P/") + char('0'+n))+char('0'+m));
                if (addr < min_pattern) min_pattern = addr;
            }
        uint64 pe[] = {
            07657777676300001LL, // 15,UTM,-2   15,WTC,1
            00300000002200000LL  //  ,UJ,
        };
        (void)pe;
        uint64 pef[] = {
            03410000100360117LL, // 7,XTA,1     ,ASN,64+15
            00040001672200000LL, //  ,ATI,14  14,UTC,
            06710000002200000LL  // 13,VJM,
        };
        uint addr = label_pattern(pef, 3*sizeof(uint64), "P/EF");
        if (addr < min_pattern) min_pattern = addr;
        if (addr != 0100000) {
            labels[addr + 3] = "P/E";
            fprintf(stderr, "Address of P/E is %05o\n", addr + 3);
        }
        uint64 p1d[] = { 0, 0, 0, 0, 0, 0, 0, 01403006014030060LL };
        addr = label_pattern(p1d, 8*sizeof(uint64), "P/1D");
        if (addr < min_pattern) min_pattern = addr;
        fprintf(stderr, "Address of P/1D is %05o\n", addr);

        code_len = min_pattern;
        fprintf(stderr, "User code ends @%05o\n", code_len);
    }

    // ==================================================================
    // Construction / driver
    // ==================================================================
    Dtran(const char * fname, Format fmt_req, uint b, bool n, bool e, bool dl, bool o) :
        fmt(fmt_req), basereg(b), baseaddr(~0u),
        nolabels(n), noequs(e), nooctal(o), nodlabels(dl)
    {
        struct stat st;
        FILE * textfd = fopen (fname, "r");
        if (! textfd) {
            fprintf (stderr, "dtran: %s not found\n", fname);
            exit(1);
        }
        stat (fname, &st);
        uint codelen = st.st_size / 6;
        if (codelen >= 32768) {
            fprintf(stderr, "File too large\n");
            exit(1);
        }

        std::vector<uint64> raw;
        raw.reserve(codelen);
        for (uint i = 0; i < codelen && !feof(textfd); ++i)
            raw.push_back(freadw(textfd));
        fclose (textfd);

        if (fmt == FMT_AUTO) {
            fmt = detect_format(raw);
            if (fmt == FMT_AUTO) {
                fprintf(stderr, "Could not detect format; use -F dms|pa|pb\n");
                exit(1);
            }
        }
        static const char * fname_of[] = { "auto", "DMS", "Pascal-A", "Pascal-B" };
        fprintf(stderr, "Format: %s\n", fname_of[fmt]);

        std::fill(memory, memory+32768, 0);

        if (fmt == FMT_DMS) {
            uint start = 0;
            if (!raw.empty() && raw[0] == BESM6_MAGIC) {
                start = 1;            // Skip magic key.
                codelen--;
            }
            for (uint i = 0; start + i < raw.size() && i < 32768; ++i)
                memory[i] = raw[start + i];
            fill_lengths_dms();
            if (codelen < comment_off) {
                fprintf(stderr, "File was too short: %d, expected %d\n", codelen, comment_off);
                exit(EXIT_FAILURE);
            }
            symtab.resize(head_len + sym_len);
            labels.resize(cmd_len + const_len + bss_len + data_len);
            return;
        }

        // Pascal-A / Pascal-B
        labels.resize(32768);
        uint base = (fmt == FMT_PASCAL_B) ? 02000 : 0;
        for (uint i = 0; i < raw.size() && base + i < 32768; ++i)
            memory[base + i] = raw[i];

        if (fmt == FMT_PASCAL_A) {
            fill_lengths_pa();
            if (codelen < total_len) {
                fprintf(stderr, "File was too short: %d, expected %d\n", codelen, total_len);
                exit(EXIT_FAILURE);
            }
            symtab.resize(04000);
            label_patterns_pa();
        } else {
            fill_lengths_pb();
            if (codelen + base < total_len) {
                fprintf(stderr, "File was too short: %d, expected %d\n", codelen, total_len);
                exit(EXIT_FAILURE);
            }
            while (memory[total_len-1] == 0) --total_len;
            symtab.resize(04000);
            label_patterns_pb();
        }
    }

    void run(bool litconst) {
        switch (fmt) {
        case FMT_DMS:
            dump_symtab();
            prtext_dms(litconst);
            prconst_dms(cmd_len, const_len, litconst);
            if (data_len) {
                printf(" ,DATA,\n");
                if (litconst) {
                    prdata();
                } else {
                    prconst_dms(cmd_len + const_len, data_len, false);
                    prsets();
                }
            }
            break;
        case FMT_PASCAL_A:
            prconst_pa(litconst);
            prtext_pa(litconst);
            break;
        case FMT_PASCAL_B:
            prtext_pb(litconst);
            prsymtab();
            break;
        default:
            break;
        }
        printf(" ,END,\n");
    }
};

static Format parse_format(const char * s) {
    if (!strcmp(s, "dms")) return FMT_DMS;
    if (!strcmp(s, "pa"))  return FMT_PASCAL_A;
    if (!strcmp(s, "pb"))  return FMT_PASCAL_B;
    fprintf(stderr, "Bad format %s, expected dms|pa|pb\n", s);
    exit(1);
}

static void load_offsets(FILE * f, std::set<int> & s, const char * what) {
    int off;
    while (1 == fscanf(f, "%i", &off))
        s.insert(off);
    fprintf(stderr, "Got %lu known %s offsets\n", s.size(), what);
}

// Parse a comma-separated list of octal numbers or start-end ranges
// (e.g. "100,200-210,377") into the set s.  Whitespace is ignored.
static void parse_offset_list(const char * list, std::set<int> & s, const char * what) {
    const char * p = list;
    for (;;) {
        while (*p == ' ' || *p == '\t') ++p;
        if (!*p) break;
        char * end;
        long start = strtol(p, &end, 8);
        if (end == p) goto bad;
        p = end;
        long stop = start;
        while (*p == ' ' || *p == '\t') ++p;
        if (*p == '-') {
            ++p;
            stop = strtol(p, &end, 8);
            if (end == p) goto bad;
            p = end;
        }
        if (stop < start) goto bad;
        for (long v = start; v <= stop; ++v)
            s.insert((int) v);
        while (*p == ' ' || *p == '\t') ++p;
        if (*p == ',') { ++p; continue; }
        if (!*p) break;
        goto bad;
    }
    return;
bad:
    fprintf(stderr, "Bad %s offset list: %s\n", what, list);
    exit(1);
}

// Read a -D command file.  Each non-blank line is a case-insensitive command
// "keyword:argument".  iso/gost/itm/text/code take an <offset list> (octal
// numbers or start-end ranges); base takes a decimal register number.
static void load_command_file(const char * path, int & basereg) {
    FILE * f = fopen(path, "r");
    if (!f) {
        fprintf(stderr, "Cannot open command file %s\n", path);
        exit(1);
    }
    char line[1024];
    int lineno = 0;
    while (fgets(line, sizeof line, f)) {
        ++lineno;
        size_t n = strlen(line);
        while (n && strchr("\r\n \t", line[n-1]))   // strip trailing space/EOL
            line[--n] = 0;
        char * s = line;
        while (*s == ' ' || *s == '\t') ++s;
        if (!*s) continue;                           // blank line
        char * colon = strchr(s, ':');
        if (!colon) {
            fprintf(stderr, "Command file %s line %d: missing ':' in \"%s\"\n",
                    path, lineno, s);
            exit(1);
        }
        *colon = 0;
        char * arg = colon + 1;
        if      (!strcasecmp(s, "iso"))  parse_offset_list(arg, isooff,  "ISO");
        else if (!strcasecmp(s, "gost")) parse_offset_list(arg, gostoff, "GOST");
        else if (!strcasecmp(s, "itm"))  parse_offset_list(arg, itmoff,  "ITM");
        else if (!strcasecmp(s, "text")) parse_offset_list(arg, textoff, "TEXT");
        else if (!strcasecmp(s, "code")) parse_offset_list(arg, codeoff, "code");
        else if (!strcasecmp(s, "base")) {
            int reg = 0;
            char extra;
            if (1 != sscanf(arg, " %d %c", &reg, &extra) || reg < 1 || reg > 15) {
                fprintf(stderr, "Command file %s line %d: bad base register \"%s\""
                        " (need 1..15)\n", path, lineno, arg);
                exit(1);
            }
            basereg = reg;
        } else {
            fprintf(stderr, "Command file %s line %d: unknown command \"%s\"\n",
                    path, lineno, s);
            exit(1);
        }
    }
    fclose(f);
}

static const char * usage = "Usage: %s [options] objfile  (try '%s -h' for help)\n";

static void print_help(const char * prog) {
    printf("Usage: %s [options] objfile\n", prog);
    printf(
"\n"
"Disassemble a BESM-6 binary into MADLEN-style pseudo-assembly. Three container\n"
"formats are understood and auto-detected; -F overrides. With -d the output is\n"
"tuned as input for the decomp.pl decompiler.\n"
"\n"
"Input format (default: auto-detect):\n"
"  -F dms|pa|pb  force the container format instead of detecting it:\n"
"                  dms  Dubna Monitor System object module\n"
"                  pa   Pascal exec, load base 0      (Pascal-Autocode)\n"
"                  pb   Pascal exec, load base 02000  (paged, Pascal-Monitor)\n"
"                Auto-detect needs a non-empty command section for dms, so\n"
"                command-less DMS modules need an explicit -F dms.\n"
"\n"
"Output form:\n"
"  -l            omit generated labels and emit a re-assemblable form\n"
"                (references become label-relative, /+N); pair with -e\n"
"  -e            expand EQUs: print literal values instead of EQU names (DMS)\n"
"  -n            suppress the /NNNN data labels (Pascal)\n"
"  -o            decimal operands and offsets instead of octal (drop B suffix)\n"
"  -c            render constant references as inline literals\n"
"\n"
"Code/data recovery:\n"
"  -R N          resolve addresses relative to base register N (1..15)\n"
"  -f off        force the code/data split at offset off (C radix: 0x.. 0.. dec)\n"
"  -E file       read extra known entry-point offsets, one per line\n"
"\n"
"Command file (supersedes the capital-letter options -R and -E):\n"
"  -D file       read commands, one per line, case-insensitive.  Text-encoding\n"
"                offsets are set only here:\n"
"                  iso:<list>   gost:<list>   itm:<list>   text:<list>\n"
"                  code:<list>  (entry points)   base:<decimal register>\n"
"                where <list> is comma-separated octal offsets or start-end\n"
"                ranges, e.g.  code:1000,2050-2077,3001\n"
"\n"
"Presets:\n"
"  -d            decompiler preset; equivalent to -e -o -c -R8\n"
"\n"
"  -h            show this help and exit\n");
}

int main (int argc, char **argv) {
    int basereg = 0;
    bool nolabels = false, noequs = false, nodlabels = false, nooctal = false, litconst = false;
    Format fmt = FMT_AUTO;
    const char * cmdfile = NULL;

    int opt;
    while ((opt = getopt(argc, argv, "hcdelnoeR:E:f:F:D:")) != -1) {
        switch (opt) {
        case 'l': nolabels = true; break;            // compilable assembly
        case 'o': nooctal = true; break;             // decimal offsets
        case 'n': nodlabels = true; break;           // no /NNNN data labels (Pascal)
        case 'e': noequs = true; break;              // expand EQUs (DMS)
        case 'c': litconst = true; break;            // const refs as literals
        case 'R':                                     // -RN base register
            basereg = 0;
            while (*optarg >= '0' && *optarg <= '9') {
                basereg = basereg*10 + (*optarg - '0');
                ++optarg;
            }
            if (basereg == 0 || basereg > 017) {
                fprintf(stderr, "Bad base reg %o, need 1 <= R <= 15\n", basereg);
                exit(1);
            }
            break;
        case 'E':
            if ((entries = fopen(optarg, "r")) == NULL) {
                fprintf(stderr, "Bad entry points file %s\n", optarg);
                exit(1);
            }
            break;
        case 'f':
            if (1 != sscanf(optarg, "%i", &forced_code_off)) {
                fprintf(stderr, "Bad forced code offset %s\n", optarg);
                exit(1);
            }
            break;
        case 'F': fmt = parse_format(optarg); break;
        case 'D': cmdfile = optarg; break;            // command file, supersedes -R -E
        case 'd':                                     // decompilation all-in-one
            noequs = true;
            nooctal = true;
            litconst = true;
            basereg = 8;
            break;
        case 'h':
            print_help(argv[0]);
            exit(EXIT_SUCCESS);
        default:
            fprintf(stderr, usage, argv[0], argv[0]);
            exit(EXIT_FAILURE);
        }
    }

    if (optind >= argc) {
        fprintf (stderr, usage, argv[0], argv[0]);
        exit (EXIT_FAILURE);
    }

    if (cmdfile) {
        // -D supersedes the capital-letter offset/entry/base options.
        basereg = 0;
        load_command_file(cmdfile, basereg);
    } else {
        if (entries) load_offsets(entries, codeoff, "entry point");
    }
    populate_itm();

    fprintf(stderr, "Decompiling file %s\n", argv[optind]);
    Dtran dtr(argv[optind], fmt, basereg, nolabels, noequs, nodlabels, nooctal);
    dtr.run(litconst);
    return 0;
}
