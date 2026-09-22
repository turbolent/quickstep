#define main SetupApplicationMain
#import "main.m"
#undef main
#include <assert.h>
#include <mach-o/loader.h>

static const char *testPackage = "/Test CD/Patch 'quoted'; name.pkg";

static void testBundle(void)
{
    NSData *binary = [NSData dataWithContentsOfFile:@"build/Setup.app/Setup"];
    NSData *expected = [NSData dataWithContentsOfFile:@"Setup.iconheader"];
    const struct mach_header *header = [binary bytes];
    const struct section *section;
    const unsigned char *icon;
    unsigned int i;
    assert([binary length] >= sizeof(*header));
    assert(header->magic == MH_MAGIC && header->filetype == MH_EXECUTE);
    assert(header->sizeofcmds <= [binary length] - sizeof(*header));
    section = getsectbynamefromheader(header, "__ICON", "__header");
    assert(section && section->offset <= [binary length]);
    assert(section->size <= [binary length] - section->offset);
    assert([expected length] && section->size >= [expected length]);
    icon = (const unsigned char *)header + section->offset;
    assert(!memcmp(icon, [expected bytes], [expected length]));
    for (i = [expected length]; i < section->size; ++i) assert(icon[i] == 0);
    section = getsectbynamefromheader(header, "__ICON", "app");
    assert(section && section->offset <= [binary length]);
    assert(section->size >= 8 && section->size <= [binary length] - section->offset);
    icon = (const unsigned char *)header + section->offset;
    assert(!memcmp(icon, "II\052\0", 4) || !memcmp(icon, "MM\0\052", 4));
}

/* Check startup ordering without opening windows or reading CD contents. */
@interface StartupProbe : SetupController
{
    int mode, phase;
}
- (id)initWithMode:(int)value;
- (void)terminate:(id)sender;
- (int)phase;
@end

@implementation StartupProbe
- (id)initWithMode:(int)value { self = [super init]; mode = value; return self; }
- (void)show { assert(phase == 0); phase = 1; }
- (BOOL)prepare
{
    assert(phase == 1); phase = 2;
    if (mode == 2) return [super prepare];
    if (mode == 1) { [self alert:@"Startup validation failed."]; return NO; }
    return YES;
}
- (void)alert:(NSString *)message
{
    assert(phase == 2);
    if (mode == 2) assert([message isEqualToString:@"Setup must be run as root."]);
    phase = 3;
}
- (void)showPackages { assert(mode == 0 && phase == 2); phase = 4; }
- (void)terminate:(id)sender
{
    assert(mode != 0 && phase == 3);
    assert([self applicationShouldTerminate:nil]);
    phase = 4;
}
- (int)phase { return phase; }
@end

static void testStartup(void)
{
    id savedApp = NSApp;
    int mode;
    for (mode = 0; mode < (geteuid() == 0 ? 2 : 3); ++mode) {
        StartupProbe *probe = [[StartupProbe alloc] initWithMode:mode];
        NSApp = probe;
        [probe applicationDidFinishLaunching:nil];
        assert([probe phase] == 1);
        [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.02]];
        assert([probe phase] == 4);
        NSApp = savedApp;
        [probe release];
    }
}

/* Stand in for both the app and task to check the focus handoff without a GUI. */
@interface LaunchProbe : NSObject
{
    int phase;
    BOOL fail;
}
- (id)initWithFailure:(BOOL)flag;
- (void)deactivate;
- (id)context;
- (void)flush;
- (void)launch;
- (void)activateIgnoringOtherApps:(BOOL)flag;
- (int)phase;
@end

@implementation LaunchProbe
- (id)initWithFailure:(BOOL)flag { self = [super init]; fail = flag; return self; }
- (void)deactivate { assert(phase == 0); phase = 1; }
- (id)context { return self; }
- (void)flush { assert(phase == 1); phase = 2; }
- (void)launch
{
    assert(phase == 2); phase = 3;
    if (fail) [NSException raise:@"LaunchFailure" format:@"Test launch failure"];
}
- (void)activateIgnoringOtherApps:(BOOL)flag { assert(flag && phase == 3); phase = 4; }
- (int)phase { return phase; }
@end

static void testFocusHandoff(void)
{
    id savedApp = NSApp;
    int fail;
    for (fail = 0; fail < 2; ++fail) {
        LaunchProbe *probe = [[LaunchProbe alloc] initWithFailure:fail];
        volatile BOOL caught = NO;
        NSApp = probe;
        NS_DURING
            launchInstallerTask((NSTask *)probe);
        NS_HANDLER
            caught = YES;
            assert([[localException name] isEqualToString:@"LaunchFailure"]);
        NS_ENDHANDLER
        NSApp = savedApp;
        assert(caught == fail && [probe phase] == (fail ? 4 : 3));
        [probe release];
    }
}

static void testReceiptLocations(void)
{
    NSDictionary *fixed = [NSDictionary dictionaryWithObject:@"NO" forKey:@"Relocatable"];
    NSDictionary *relocatable = [NSDictionary dictionaryWithObject:@"YES" forKey:@"Relocatable"];
    NSString *info = @"Version 1\nDefaultLocation /private/Devices\n";
    assert(receiptLocationMatches(fixed, info, @"/private/Devices"));
    assert(!receiptLocationMatches(fixed, info, @"/"));
    assert(!receiptLocationMatches(fixed, info, @"private/Devices"));
    assert(receiptLocationMatches(relocatable, info, @"/elsewhere"));
    assert(!receiptLocationMatches(relocatable, info, @"relative"));
    assert(receiptLocationMatches(fixed, @"Version 1\n", @"/"));
    assert([infoField(@"version 2\r\ndefaultlocation /private/Devices\r\n", "Version") isEqual:@"2"]);
}

static void testPackageDescriptions(void)
{
    NSString *root = [NSString stringWithFormat:@"./build/description-test-%ld", (long)getpid()];
    NSString *cd = [root stringByAppendingPathComponent:@"CD"];
    NSString *receipts = [root stringByAppendingPathComponent:@"Receipts"];
    NSArray *directories = [NSArray arrayWithObjects:@"CD", @"CD/Tools.pkg", @"CD/Tools.pkg/English.lproj",
        @"Receipts", @"Receipts/Tools.pkg", @"Receipts/Libs.pkg", nil];
    NSArray *files = [NSArray arrayWithObjects:@"CD/Tools.pkg/Tools.info", @"CD/Tools.pkg/English.lproj/Tools.info",
        @"Receipts/Tools.pkg/Tools.info", @"Receipts/Libs.pkg/Libs.info", nil];
    NSArray *contents = [NSArray arrayWithObjects:@"Title Tools\nDescription Root description\n",
        @"Title Developer Tools\r\nDescription   Compilers and debuggers.  \r\n",
        @"Title Old tools\nDescription Older receipt description\n",
        @"description Development libraries.\n", nil];
    NSDictionary *single = [NSDictionary dictionaryWithObject:[NSArray arrayWithObject:@"Tools"] forKey:@"Packages"];
    NSDictionary *group = [NSDictionary dictionaryWithObject:
        [NSArray arrayWithObjects:@"Tools", @"Libs", @"Missing", nil] forKey:@"Packages"];
    NSDictionary *missing = [NSDictionary dictionaryWithObject:[NSArray arrayWithObject:@"Missing"] forKey:@"Packages"];
    int i;
    assert([NSView instancesRespondToSelector:@selector(setToolTip:)]);
    assert(mkdir([root cString], 0700) == 0);
    for (i = 0; i < [directories count]; ++i)
        assert(mkdir([[root stringByAppendingPathComponent:[directories objectAtIndex:i]] cString], 0700) == 0);
    for (i = 0; i < [files count]; ++i)
        assert([[contents objectAtIndex:i] writeToFile:[root stringByAppendingPathComponent:[files objectAtIndex:i]]
                                           atomically:YES]);
    assert([choiceDescription(single, cd, receipts) isEqual:@"Compilers and debuggers."]);
    assert([choiceDescription(group, cd, receipts) isEqual:
        @"Developer Tools: Compilers and debuggers.\n\nLibs: Development libraries."]);
    assert(choiceDescription(missing, cd, receipts) == nil);
    /* Prefer English metadata, then root metadata; missing descriptions may
     * fall back to an installed receipt. Missing metadata is simply omitted. */
    assert(unlink([[root stringByAppendingPathComponent:[files objectAtIndex:1]] cString]) == 0);
    assert([choiceDescription(single, cd, receipts) isEqual:@"Root description"]);
    assert([@"Title Tools\nDescription   \n" writeToFile:
        [root stringByAppendingPathComponent:[files objectAtIndex:0]] atomically:YES]);
    assert([choiceDescription(single, cd, receipts) isEqual:@"Older receipt description"]);
    for (i = 0; i < [files count]; ++i)
        if (i != 1) assert(unlink([[root stringByAppendingPathComponent:[files objectAtIndex:i]] cString]) == 0);
    for (i = [directories count] - 1; i >= 0; --i)
        assert(rmdir([[root stringByAppendingPathComponent:[directories objectAtIndex:i]] cString]) == 0);
    assert(rmdir([root cString]) == 0);
}

static void testPackageListResizing(void)
{
    NSClipView *clip = [[NSClipView alloc] initWithFrame:NSMakeRect(0, 0, 410, 245)];
    PackageListView *list = [[PackageListView alloc] initWithFrame:NSMakeRect(0, 0, 410, 245)];
    NSView *row = [[NSView alloc] initWithFrame:NSMakeRect(14, 24, 396, 20)];
    [list setAutoresizingMask:NSViewWidthSizable];
    [row setAutoresizingMask:NSViewWidthSizable];
    [list addSubview:row]; [clip setDocumentView:list];
    [clip setFrameSize:NSMakeSize(610, 445)];
    assert([list isFlipped] && NSWidth([list frame]) == 610);
    assert(NSWidth([row frame]) == 596 && NSMinY([row frame]) == 24);
    assert(NSMinY([clip bounds]) == 0);
    /* Growing the viewport must not leave short lists at its bottom. */
    [clip setFrameSize:NSMakeSize(350, 545)];
    assert(NSWidth([list frame]) == 350 && NSWidth([row frame]) == 336);
    assert(NSMinY([clip bounds]) == 0);
    /* Long lists keep their row spacing and can still scroll after a resize. */
    [list setFrameSize:NSMakeSize(350, 900)];
    [clip setFrameSize:NSMakeSize(450, 145)];
    [clip scrollToPoint:NSMakePoint(0, 500)];
    assert(NSMinY([clip bounds]) == 500 && NSWidth([list frame]) == 450);
    assert(NSMinY([row frame]) == 24 && NSHeight([row frame]) == 20);
    [clip release]; [list release]; [row release];
}

@interface CloseProbe : SetupController
{
    BOOL terminated;
    int alerts;
}
- (id)initWithBusy:(BOOL)flag;
- (void)terminate:(id)sender;
- (BOOL)terminated;
- (int)alerts;
@end

@implementation CloseProbe
- (id)initWithBusy:(BOOL)flag { self = [super init]; busy = flag; return self; }
- (void)terminate:(id)sender { terminated = [self applicationShouldTerminate:nil]; }
- (void)alert:(NSString *)message { ++alerts; }
- (BOOL)terminated { return terminated; }
- (int)alerts { return alerts; }
@end

static void testWindowClose(void)
{
    id savedApp = NSApp;
    int flag;
    for (flag = 0; flag < 2; ++flag) {
        CloseProbe *probe = [[CloseProbe alloc] initWithBusy:flag];
        NSApp = probe;
        assert(![probe windowShouldClose:nil]);
        assert([probe terminated] == !flag && [probe alerts] == flag);
        NSApp = savedApp;
        [probe release];
    }
}

/* The child checks argument boundaries and reports its inherited credentials.
 * It never opens a GUI or package, and exits when its input pipe closes. */
static NSTask *startStub(void)
{
    NSTask *task = newInstallerTask(text(testPackage));
    NSString *expected = [NSString stringWithFormat:@"%lu:%lu\n",
        (unsigned long)getuid(), (unsigned long)geteuid()];
    NSData *output;
    assert([[task launchPath] isEqualToString:@"/NextAdmin/Installer.app/Installer"]);
    assert(([[task arguments] isEqual:[NSArray arrayWithObjects:@"-NXOpen", text(testPackage), nil]]));
    [task setLaunchPath:@"./build/test_controller"];
    [task setStandardInput:[NSPipe pipe]];
    [task setStandardOutput:[NSPipe pipe]];
    launchInstallerTask(task);
    output = [[[task standardOutput] fileHandleForReading] readDataOfLength:[expected length]];
    assert([output isEqual:[expected dataUsingEncoding:NSASCIIStringEncoding]]);
    return task;
}

static void finishStub(NSTask *task)
{
    [[[task standardInput] fileHandleForWriting] closeFile];
    [task waitUntilExit];
    assert([task terminationStatus] == 0);
}

@interface TestController : SetupController
{
    int advances, alerts;
    BOOL hasReceipt, driveSequence;
}
- (id)initWithTask:(NSTask *)task;
- (void)publishReceipt;
- (void)useOldReceipt;
- (void)driveSequence;
- (void)keepOnlyCurrentPackage;
- (BOOL)isBusy;
- (BOOL)hasActivePackage;
- (int)advances;
- (int)alerts;
@end

@implementation TestController
- (id)initWithTask:(NSTask *)task
{
    self = [super init];
    active = [@"Patch" retain]; busy = YES;
    installerTask = [task retain];
    packageRoot = [@"./build/nonexistent-packages" retain];
    installed = [[NSMutableSet alloc] init];
    selected = [[NSMutableSet alloc] initWithObjects:@"Patch", @"Later", nil];
    pending = [[NSMutableArray alloc] initWithObjects:@"Patch", @"Later", nil];
    return self;
}
- (void)publishReceipt { hasReceipt = YES; }
- (void)useOldReceipt { hasReceipt = YES; beforeReceipt = [[self matchingReceipt:active] retain]; }
- (void)driveSequence { driveSequence = YES; }
- (void)keepOnlyCurrentPackage
{
    [pending removeLastObject]; [selected removeObject:@"Later"];
    driveSequence = YES;
}
- (BOOL)isBusy { return busy; }
- (BOOL)hasActivePackage { return active != nil; }
- (int)advances { return advances; }
- (int)alerts { return alerts; }
- (NSDictionary *)matchingReceipt:(NSString *)name
{
    return hasReceipt ? [NSDictionary dictionaryWithObject:@"installed" forKey:@"status"] : nil;
}
- (void)scanInstalled { }
- (void)updateRows { }
- (void)message:(NSString *)message { }
- (void)alert:(NSString *)message { ++alerts; }
- (void)advance
{
    if (stopRequested) [super advance];
    else {
        ++advances;
        if (driveSequence) [super advance];
    }
}
@end

@interface ActionController : TestController
{
    int actions;
}
- (void)setCommand:(NSArray *)command;
- (void)alreadyInstalled;
- (void)waitForAction;
- (int)actions;
- (BOOL)actionComplete;
- (void)missingPrerequisite;
- (void)useTestLogAndDirectory;
- (BOOL)logContains:(const char *)value;
@end

@implementation ActionController
- (id)initWithTask:(NSTask *)task
{
    self = [super initWithTask:task];
    [self keepOnlyCurrentPackage];
    available = [[NSMutableSet alloc] initWithObjects:@"Tools", @"Patch", nil];
    postInstallDone = [[NSMutableSet alloc] init];
    [installed addObject:@"Tools"];
    [self setCommand:[NSArray arrayWithObjects:@"/bin/sh", @"-c", @"exit 0", nil]];
    return self;
}
- (void)setCommand:(NSArray *)command
{
    NSMutableDictionary *plist = [[[NSDictionary dictionaryWithContentsOfFile:@"test_catalog.plist"] mutableCopy] autorelease];
    NSArray *packages = [plist objectForKey:@"Packages"];
    NSMutableDictionary *package = [[[packages objectAtIndex:0] mutableCopy] autorelease];
    NSString *error = nil;
    [package setObject:[NSArray arrayWithObject:@"Tools"] forKey:@"Dependencies"];
    [package setObject:command forKey:@"PostInstall"];
    [plist setObject:@"NO" forKey:@"FixPICBug"];
    [plist setObject:[NSArray arrayWithObjects:[packages objectAtIndex:2], package, nil] forKey:@"Packages"];
    [plist setObject:[NSArray array] forKey:@"Choices"];
    [model release]; model = [[SetupModel alloc] initWithPropertyList:plist error:&error];
    assert(model && !error);
}
- (void)alreadyInstalled
{
    [installed addObject:@"Patch"];
    [active release]; active = nil;
}
- (void)startPostInstall { ++actions; [super startPostInstall]; }
- (void)waitForAction { assert(postInstallTask); [postInstallTask waitUntilExit]; }
- (int)actions { return actions; }
- (BOOL)actionComplete { return [[self completed] containsObject:@"Patch"]; }
- (void)missingPrerequisite
{
    [installed removeObject:@"Tools"]; [available removeObject:@"Tools"];
    busy = NO;
}
- (void)useTestLogAndDirectory
{
    cdRoot = [@"/private/tmp" retain];
    log = tmpfile(); assert(log);
}
- (BOOL)logContains:(const char *)value
{
    char data[1024];
    int count;
    rewind(log); count = fread(data, 1, sizeof(data) - 1, log); data[count] = 0;
    fseek(log, 0, SEEK_END);
    return strstr(data, value) != NULL;
}
@end

static void testPostInstall(void)
{
    NSTask *stub = startStub();
    ActionController *controller = [[ActionController alloc] initWithTask:stub];
    [controller publishReceipt]; [controller poll:nil]; [controller poll:nil];
    assert([controller isBusy] && [controller actions] == 0 && ![controller actionComplete]);
    finishStub(stub); [controller poll:nil];
    assert([controller isBusy] && [controller actions] == 1 && [controller alerts] == 0);
    [controller waitForAction]; [controller poll:nil];
    assert(![controller isBusy] && [controller actionComplete] && [controller alerts] == 1);
    [controller release]; [stub release];

    /* Cancellation never runs an action just because a package was selected. */
    stub = startStub(); controller = [[ActionController alloc] initWithTask:stub];
    finishStub(stub); [controller poll:nil];
    assert(![controller isBusy] && [controller actions] == 0);
    [controller release]; [stub release];

    /* Existing receipts still need the idempotent action; failure keeps Retry
     * available without reinstalling the package or launching Installer. */
    controller = [[ActionController alloc] initWithTask:nil];
    [controller alreadyInstalled];
    [controller setCommand:[NSArray arrayWithObjects:@"/bin/sh", @"-c", @"exit 7", nil]];
    [controller advance]; [controller waitForAction]; [controller poll:nil];
    assert(![controller isBusy] && ![controller actionComplete] && [controller actions] == 1);
    [controller setCommand:[NSArray arrayWithObjects:@"/bin/sh", @"-c", @"exit 0", nil]];
    [controller install:nil]; [controller waitForAction]; [controller poll:nil];
    assert(![controller isBusy] && [controller actionComplete] && [controller actions] == 2);
    [controller release];

    controller = [[ActionController alloc] initWithTask:nil];
    [controller alreadyInstalled];
    [controller setCommand:[NSArray arrayWithObject:@"/nonexistent/Setup-test-helper"]];
    [controller advance];
    assert(![controller isBusy] && ![controller actionComplete] && [controller alerts] == 1);
    [controller release];

    controller = [[ActionController alloc] initWithTask:nil];
    [controller alreadyInstalled]; [controller missingPrerequisite];
    [controller install:nil];
    assert(![controller isBusy] && [controller actions] == 0 && [controller alerts] == 1);
    [controller release];

    controller = [[ActionController alloc] initWithTask:nil];
    [controller alreadyInstalled]; [controller useTestLogAndDirectory];
    [controller setCommand:[NSArray arrayWithObjects:@"/bin/sh", @"-c", @"pwd; echo helper-stderr >&2", nil]];
    [controller advance]; [controller waitForAction]; [controller poll:nil];
    assert([controller actionComplete]);
    assert([controller logContains:"/private/tmp"] && [controller logContains:"helper-stderr"]);
    [controller release];

    /* Stop waits for Installer exit and the installed package's action. */
    stub = startStub(); controller = [[ActionController alloc] initWithTask:stub];
    [controller publishReceipt]; [controller poll:nil]; [controller poll:nil];
    [controller quit:nil]; assert([controller isBusy] && [controller actions] == 0);
    finishStub(stub); [controller poll:nil];
    assert([controller isBusy] && [controller actions] == 1);
    [controller quit:nil]; [controller waitForAction]; [controller poll:nil];
    assert(![controller isBusy] && [controller actionComplete]);
    [controller release]; [stub release];
}

int main(int argc, char **argv)
{
    NSAutoreleasePool *pool;
    TestController *controller;
    NSTask *stub, *unrelated;
    if (argc == 3 && !strcmp(argv[1], "-NXOpen")) {
        char byte;
        assert(!strcmp(argv[2], testPackage));
        printf("%lu:%lu\n", (unsigned long)getuid(), (unsigned long)geteuid());
        fflush(stdout);
        read(0, &byte, 1);
        return 0;
    }
    pool = [[NSAutoreleasePool alloc] init];
    testBundle();
    testStartup();
    testFocusHandoff();
    testReceiptLocations();
    testPackageDescriptions();
    testPackageListResizing();
    testWindowClose();
    testPostInstall();
    stub = startStub(); unrelated = startStub();
    controller = [[TestController alloc] initWithTask:stub];
    [controller poll:nil]; assert([controller isBusy]);
    finishStub(stub);
    [controller poll:nil];
    assert(![controller isBusy] && [controller advances] == 0);
    assert([unrelated isRunning]);
    [controller release];
    finishStub(unrelated); [unrelated release];

    controller = [[TestController alloc] initWithTask:stub];
    [controller quit:nil];
    assert(![controller isBusy] && [controller advances] == 0);
    [controller release];
    /* Quitting with an old receipt is not a successful installation. */
    controller = [[TestController alloc] initWithTask:stub];
    [controller useOldReceipt]; [controller poll:nil];
    assert(![controller isBusy] && [controller advances] == 0);
    [controller release];
    /* A fresh receipt still counts if Installer has already exited. */
    controller = [[TestController alloc] initWithTask:stub];
    [controller publishReceipt]; [controller poll:nil];
    assert([controller advances] == 1); [controller release];
    /* Stop suppresses the next package, including on a successful exit. */
    controller = [[TestController alloc] initWithTask:stub];
    [controller publishReceipt]; [controller quit:nil];
    assert(![controller isBusy] && [controller advances] == 0);
    [controller release]; [stub release];

    /* Stop never kills a running installation. Its exit unlocks Setup. */
    stub = startStub();
    controller = [[TestController alloc] initWithTask:stub];
    [controller quit:nil];
    assert([controller isBusy] && [stub isRunning]);
    finishStub(stub); [controller poll:nil];
    assert(![controller isBusy] && [controller advances] == 0);
    [controller release]; [stub release];

    /* Require two stable receipt snapshots while the child is still running.
     * Wait for that child to quit before trying the next package. */
    stub = startStub();
    controller = [[TestController alloc] initWithTask:stub];
    [controller driveSequence]; [controller publishReceipt];
    [controller poll:nil]; assert([controller advances] == 0);
    [controller poll:nil];
    assert([controller isBusy] && ![controller hasActivePackage]);
    assert([controller advances] == 1 && [stub isRunning]);
    [controller poll:nil]; assert([controller advances] == 1);
    finishStub(stub); [controller poll:nil];
    /* The next package is deliberately absent, so no real Installer launches. */
    assert([controller advances] == 2 && ![controller isBusy]);
    [controller release]; [stub release];

    /* The final receipt must not show the completion dialog before exit. */
    stub = startStub();
    controller = [[TestController alloc] initWithTask:stub];
    [controller keepOnlyCurrentPackage]; [controller publishReceipt];
    [controller poll:nil]; [controller poll:nil];
    assert([controller isBusy] && ![controller hasActivePackage]);
    assert([controller alerts] == 0 && [stub isRunning]);
    [controller poll:nil]; assert([controller alerts] == 0);
    finishStub(stub); [controller poll:nil];
    assert(![controller isBusy] && [controller alerts] == 1);
    [controller poll:nil]; assert([controller alerts] == 1);
    [controller release]; [stub release];

    /* Stop also waits for a completed Installer to close. */
    stub = startStub();
    controller = [[TestController alloc] initWithTask:stub];
    [controller driveSequence]; [controller publishReceipt];
    [controller poll:nil]; [controller poll:nil]; [controller quit:nil];
    assert([controller isBusy] && [controller advances] == 1);
    assert([controller alerts] == 0 && [stub isRunning]);
    finishStub(stub); [controller poll:nil];
    assert(![controller isBusy] && [controller alerts] == 1);
    assert([controller advances] == 1);
    [controller release]; [stub release];

    stub = startStub();
    controller = [[TestController alloc] initWithTask:stub];
    [controller publishReceipt]; [controller quit:nil];
    assert([controller isBusy]); [controller poll:nil];
    assert([controller isBusy] && [controller advances] == 0);
    assert([controller alerts] == 0 && [stub isRunning]);
    finishStub(stub); [controller poll:nil];
    assert(![controller isBusy] && [controller alerts] == 1);
    assert([controller advances] == 0);
    [controller release]; [stub release];
    printf("Setup controller tests passed (child uid %lu, euid %lu).\n",
        (unsigned long)getuid(), (unsigned long)geteuid());
    [pool release];
    return 0;
}
