#define main SetupApplicationMain
#import "main.m"
#undef main
#include <assert.h>

static const char *testPackage = "/Test CD/Patch 'quoted'; name.pkg";

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
    testFocusHandoff();
    testReceiptLocations();
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
