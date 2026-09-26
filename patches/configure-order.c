/* OPENSTEP 4.2 Configure, Intel slice SHA-256 77e80715...945f.
 * No new runtime or Objective-C metadata is needed:
 * use the executable's existing, runtime-fixed selector and class references.
 * The two saved strings occupy unused, zero-filled __DATA segment padding.
 */
typedef unsigned int word;
#define REF(address) (*(volatile word *)(address))
#define SEND ((word (*)(word, word, ...))0x05003477)
#define DRIVER_BUNDLE REF(0x1738b0)
#define ARRAY_CLASS REF(0x1738c4)
#define SAVED ((word *)0x171ff0)
#define ACTIVE 0x172910
#define BOOT 0x17291c
#define SPACE 0x172c40
#define SYSTEM_DICT REF(0x173194)
#define GET REF(0x172eb0)
#define SET REF(0x172f8c)
#define RETAIN REF(0x172ea4)
#define RELEASE REF(0x172f10)
#define SPLIT REF(0x17319c)
#define ARRAY REF(0x172f70)
#define COUNT REF(0x172e8c)
#define AT REF(0x172eac)
#define CONTAINS REF(0x173160)
#define ADD_UNIQUE REF(0x172ff4)
#define JOIN REF(0x17322c)

static word previous_value(word key, word fallback)
{
    /* Installation mode deliberately skips readInstances. Read the saved
     * System instance explicitly so a customized order wins over Default.
     * Reuse Configure's own path formatting and strings-file parser.
     */
    word bundle = SEND(DRIVER_BUNDLE, REF(0x172f64));
    word directory = SEND(bundle, REF(0x173064));
    word path = SEND(REF(0x1738b4), REF(0x172f54), 0x172c4c, directory, 0);
    word text = SEND(REF(0x1738b4), REF(0x1731dc), path);
    word table = SEND(text, REF(0x1731e0));
    word value = SEND(table, GET, key);
    return value ? value : SEND(fallback, GET, key);
}

/* Replace just the two installation-mode insertSystemKey:value: calls.
 * Retain the old value before the original call clears it; leave detection,
 * instance creation, and family grouping intact.
 */
__attribute__((section(".text.capture")))
word capture(word receiver, word selector, word key, word value)
{
    word index = key == BOOT;
    word dict = SEND(DRIVER_BUNDLE, SYSTEM_DICT);
    word old = previous_value(key, dict);
    old = SEND(old, RETAIN);
    SEND(SAVED[index], RELEASE);
    SAVED[index] = old;
    return SEND(receiver, selector, key, value);
}

static void restore(word dict, word key, word original)
{
    word current, previous, ordered, i, n, name;
    if (!original || !dict)
        return;
    current = SEND(SEND(dict, GET, key), SPLIT);
    previous = SEND(original, SPLIT);
    ordered = SEND(ARRAY_CLASS, ARRAY);
    n = SEND(previous, COUNT);
    for (i = 0; i < n; ++i) {
        name = SEND(previous, AT, i);
        /* OPENSTEP BOOL is a byte; upper EAX bits are not part of the result. */
        if ((unsigned char)SEND(current, CONTAINS, name))
            SEND(ordered, ADD_UNIQUE, name);
    }
    /* Preserve new entries too, in their existing relative order. */
    n = SEND(current, COUNT);
    for (i = 0; i < n; ++i)
        SEND(ordered, ADD_UNIQUE, SEND(current, AT, i));
    SEND(dict, SET, SEND(ordered, JOIN, SPACE), key);
}

/* First-time setup ignores Instance*.table and builds its list from live
 * device discovery. Target-only drivers must also survive that step, even
 * when their hardware probe fails. The installer explicitly names them in
 * System.config; do not retain other undetected drivers or re-add a driver
 * removed from the saved activation lists. Ordinary configuration is unchanged.
 */
__attribute__((noinline))
static void include_install_drivers(word drivers)
{
    word dict = SEND(DRIVER_BUNDLE, SYSTEM_DICT);
    word key = SEND(REF(0x1738b4), REF(0x172f50), "Quickstep Install Drivers");
    word names = SEND(previous_value(key, dict), SPLIT);
    word boot = SEND(SAVED[1], SPLIT);
    word active = SEND(SAVED[0], SPLIT);
    word n = SEND(names, COUNT), i, name, bundle;
    for (i = 0; i < n; ++i) {
        name = SEND(names, AT, i);
        if (!(unsigned char)SEND(boot, CONTAINS, name) &&
            !(unsigned char)SEND(active, CONTAINS, name))
            continue;
        bundle = SEND(DRIVER_BUNDLE, REF(0x173074), name);
        if (bundle)
            SEND(drivers, ADD_UNIQUE, bundle);
    }
}

/* Wrap the existing _createDriverListsFrom: dispatch. In ordinary mode no
 * snapshots exist, so dispatch and return without touching the system table.
 */
__attribute__((section(".text.finish")))
word finish(word receiver, word selector, word drivers)
{
    word result;
    if (SAVED[0] || SAVED[1])
        include_install_drivers(drivers);
    result = SEND(receiver, selector, drivers);
    if (SAVED[0] || SAVED[1]) {
        word dict = SEND(DRIVER_BUNDLE, SYSTEM_DICT);
        restore(dict, ACTIVE, SAVED[0]);
        restore(dict, BOOT, SAVED[1]);
        SEND(SAVED[0], RELEASE);
        SEND(SAVED[1], RELEASE);
        SAVED[0] = SAVED[1] = 0;
    }
    return result;
}
