#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <libc.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include "SetupModel.h"

static NSString *text(const char *s) { return [NSString stringWithCString:s]; }

/* Launch directly: Workspace may reuse an Installer belonging to the desktop
 * user instead of inheriting Setup's root credentials. */
static NSTask *newInstallerTask(NSString *package)
{
    NSTask *task = [[NSTask alloc] init];
    [task setLaunchPath:@"/NextAdmin/Installer.app/Installer"];
    [task setArguments:[NSArray arrayWithObjects:@"-NXOpen", package, nil]];
    return task;
}

static void launchInstallerTask(NSTask *task)
{
    /* Installer activates conditionally at startup; yield focus first, as
     * Workspace does, and send the change to the Window Server before launch. */
    [NSApp deactivate];
    [[NSApp context] flush];
    NS_DURING
        [task launch];
    NS_HANDLER
        [NSApp activateIgnoringOtherApps:YES];
        [localException raise];
    NS_ENDHANDLER
}

static NSString *trim(NSString *s)
{
    const char *start;
    size_t length;
    if (!s) return nil;
    start = [s cString];
    while (*start && isspace((unsigned char)*start)) ++start;
    length = strlen(start);
    while (length && isspace((unsigned char)start[length - 1])) --length;
    return [NSString stringWithCString:start length:length];
}

static NSString *infoPath(NSString *bundle, NSString *name)
{
    NSString *leaf = [name stringByAppendingString:@".info"];
    NSString *path = [[bundle stringByAppendingPathComponent:@"English.lproj"]
                     stringByAppendingPathComponent:leaf];
    if (![[NSFileManager defaultManager] fileExistsAtPath:path])
        path = [bundle stringByAppendingPathComponent:leaf];
    return path;
}

static NSString *infoVersion(NSString *contents)
{
    NSEnumerator *lines = [[contents componentsSeparatedByString:@"\n"] objectEnumerator];
    NSString *line;
    NSString *result = nil;
    while ((line = [lines nextObject])) {
        const char *value = [line cString];
        while (*value && isspace((unsigned char)*value)) ++value;
        if (!strncmp(value, "Version", 7) && isspace((unsigned char)value[7]))
            result = trim(text(value + 7));
    }
    return [result length] ? result : nil;
}

/* Identity plus content prevents an old 'installed' status being mistaken for
 * this run. Read the file through an fd and reject symlinks and changing reads. */
static NSDictionary *fileStamp(NSString *path)
{
    struct stat before, after, opened;
    int fd;
    int amount;
    size_t used = 0;
    char *bytes;
    NSData *data;
    NSString *identity;
    if (lstat([path cString], &before) || (before.st_mode & S_IFMT) != S_IFREG ||
        before.st_size < 0 || before.st_size > 65536) return nil;
    fd = open([path cString], O_RDONLY);
    if (fd < 0) return nil;
    if (fstat(fd, &opened) || opened.st_ino != before.st_ino || opened.st_dev != before.st_dev) {
        close(fd); return nil;
    }
    bytes = malloc(before.st_size + 1);
    if (!bytes) { close(fd); return nil; }
    while (used < (size_t)before.st_size) {
        amount = read(fd, bytes + used, before.st_size - used);
        if (amount <= 0) break;
        used += amount;
    }
    close(fd);
    if (lstat([path cString], &after) || used != (size_t)before.st_size ||
        after.st_ino != before.st_ino || after.st_dev != before.st_dev ||
        after.st_size != before.st_size || after.st_mtime != before.st_mtime ||
        after.st_ctime != before.st_ctime) { free(bytes); return nil; }
    data = [NSData dataWithBytes:bytes length:used]; free(bytes);
    identity = [NSString stringWithFormat:@"%lu:%lu:%lu:%lu:%lu",
        (unsigned long)after.st_dev, (unsigned long)after.st_ino,
        (unsigned long)after.st_size, (unsigned long)after.st_mtime, (unsigned long)after.st_ctime];
    return [NSDictionary dictionaryWithObjectsAndKeys:identity, @"identity", data, @"data", nil];
}

static NSDictionary *receiptStamp(NSString *bundle, NSString *name)
{
    NSDictionary *status = fileStamp([bundle stringByAppendingPathComponent:
                                     [name stringByAppendingString:@".status"]]);
    NSDictionary *location = fileStamp([bundle stringByAppendingPathComponent:
                                       [name stringByAppendingString:@".location"]]);
    NSDictionary *info = fileStamp(infoPath(bundle, name));
    if (!status || !location || !info) return nil;
    return [NSDictionary dictionaryWithObjectsAndKeys:status, @"status", location, @"location", info, @"info", nil];
}

static NSString *stampText(NSDictionary *stamp, NSString *key)
{
    NSData *data = [[stamp objectForKey:key] objectForKey:@"data"];
    if (!data) return nil;
    return trim([[[NSString alloc] initWithData:data encoding:NSNEXTSTEPStringEncoding] autorelease]);
}

@interface SetupController : NSObject
{
    NSWindow *window;
    NSTextField *status;
    NSButton *installButton, *quitButton;
    NSMutableArray *rows, *visibleChoices, *pending;
    SetupModel *model;
    NSString *cdRoot, *packageRoot, *active;
    NSDictionary *beforeReceipt, *candidateReceipt;
    NSTimer *timer;
    NSTask *picTask, *installerTask;
    NSPipe *picOutput;
    NSMutableSet *available, *installed, *selected;
    BOOL busy, stopRequested, picDone, restartNeeded;
    FILE *log;
    NSString *logPath;
}
- (BOOL)prepare;
- (void)show;
- (NSDictionary *)matchingReceipt:(NSString *)name;
- (void)advance;
- (void)selectionChanged:(id)sender;
- (void)install:(id)sender;
- (void)quit:(id)sender;
- (void)poll:(NSTimer *)sender;
- (void)checkInstaller;
- (BOOL)applicationShouldTerminate:(NSApplication *)application;
@end

@implementation SetupController
- (void)message:(NSString *)message
{
    [status setStringValue:message];
    if (log) { fprintf(log, "%s\n", [message cString]); fflush(log); }
}
- (void)alert:(NSString *)message
{
    if (log) { fprintf(log, "%s\n", [message cString]); fflush(log); }
    NSRunAlertPanel(@"Setup", @"%@", @"OK", nil, nil, message);
}
- (NSString *)receipt:(NSString *)name
{
    return [@"/NextLibrary/Receipts" stringByAppendingPathComponent:
            [name stringByAppendingString:@".pkg"]];
}
- (NSDictionary *)matchingReceipt:(NSString *)name
{
    NSDictionary *package = [model package:name];
    NSDictionary *stamp = receiptStamp([self receipt:name], name);
    NSString *location = stampText(stamp, @"location");
    NSString *version = infoVersion(stampText(stamp, @"info"));
    if (!stamp || ![stampText(stamp, @"status") isEqualToString:@"installed"] ||
        ![location hasPrefix:@"/"] ||
        (![[package objectForKey:@"Relocatable"] isEqual:@"YES"] && ![location isEqualToString:@"/"]) ||
        ![version isEqualToString:[package objectForKey:@"Version"]]) return nil;
    return stamp;
}
- (void)scanInstalled
{
    NSEnumerator *packages = [[model packages] objectEnumerator];
    NSDictionary *package;
    [installed removeAllObjects];
    while ((package = [packages nextObject])) {
        NSString *name = [package objectForKey:@"Name"];
        if ([self matchingReceipt:name]) [installed addObject:name];
    }
}
- (BOOL)prepare
{
    NSString *bundle = [[NSBundle mainBundle] bundlePath];
    NSString *error = nil;
    NSFileManager *files = [NSFileManager defaultManager];
    struct stat rootStat, cdStat;
    int i, fd;
    BOOL directory;
    if (geteuid() != 0) {
        [self alert:@"Setup must be run as root."];
        return NO;
    }
    cdRoot = [[bundle stringByDeletingLastPathComponent] copy];
    packageRoot = [[cdRoot stringByAppendingPathComponent:@"NextCD/Packages"] copy];
    if (![files fileExistsAtPath:packageRoot isDirectory:&directory] || !directory ||
        stat("/", &rootStat) || stat([cdRoot cString], &cdStat) || rootStat.st_dev == cdStat.st_dev) {
        [self alert:@"Boot and configure the installed system first, mount the CD, and open Setup.app on that CD."];
        return NO;
    }
    NS_DURING
        NSDictionary *plist = [NSDictionary dictionaryWithContentsOfFile:
                               [bundle stringByAppendingPathComponent:@"Setup.plist"]];
        model = [[SetupModel alloc] initWithPropertyList:plist error:&error];
    NS_HANDLER
        error = @"Cannot read Setup.plist.";
    NS_ENDHANDLER
    if (!model) {
        [self alert:[NSString stringWithFormat:@"%@\nInclude this bundle using build_cd.py --setup-app.", error]];
        return NO;
    }
    available = [[NSMutableSet alloc] init]; installed = [[NSMutableSet alloc] init];
    selected = [[NSMutableSet alloc] init];
    rows = [[NSMutableArray alloc] init]; visibleChoices = [[NSMutableArray alloc] init];
    logPath = [[NSString stringWithFormat:@"/private/tmp/Setup.%ld.log", (long)getpid()] retain];
    fd = open([logPath cString], O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd < 0) { [self alert:@"Cannot create the Setup log in /private/tmp."]; return NO; }
    log = fdopen(fd, "w");
    if (!log) { close(fd); [self alert:@"Cannot open the Setup log."]; return NO; }
    for (i = 0; i < [[model packages] count]; ++i) {
        NSDictionary *package = [[model packages] objectAtIndex:i];
        NSString *name = [package objectForKey:@"Name"];
        NSString *path = [packageRoot stringByAppendingPathComponent:
                          [name stringByAppendingString:@".pkg"]];
        if ([files fileExistsAtPath:path isDirectory:&directory] && directory &&
            [infoVersion([NSString stringWithContentsOfFile:infoPath(path, name)])
                isEqualToString:[package objectForKey:@"Version"]]) [available addObject:name];
    }
    [self scanInstalled];
    for (i = 0; i < [[model choices] count]; ++i) {
        NSDictionary *choice = [[model choices] objectAtIndex:i];
        if ([model choiceVisible:choice available:available installed:installed]) {
            [visibleChoices addObject:choice];
            if ([[choice objectForKey:@"DefaultSelected"] isEqual:@"YES"])
                [selected addObjectsFromArray:[choice objectForKey:@"Packages"]];
        }
    }
    [selected setSet:[NSSet setWithArray:[model plan:selected available:available installed:installed
                                                   missing:[NSMutableSet set]]]];
    return YES;
}
- (NSTextField *)label:(NSString *)title frame:(NSRect)frame bold:(BOOL)bold in:(NSView *)parent
{
    NSTextField *field = [[[NSTextField alloc] initWithFrame:frame] autorelease];
    [field setEditable:NO]; [field setSelectable:NO]; [field setBezeled:NO];
    [field setDrawsBackground:NO];
    [field setFont:bold ? [NSFont boldSystemFontOfSize:12] : [NSFont systemFontOfSize:12]];
    [field setStringValue:title]; [parent addSubview:field];
    return field;
}
- (BOOL)needsPIC
{
    NSEnumerator *names = [installed objectEnumerator];
    NSString *name;
    if (![model fixPIC] || picDone) return NO;
    while ((name = [names nextObject]))
        if ([[[model package:name] objectForKey:@"FixPICAfter"] isEqual:@"YES"]) return YES;
    return NO;
}
- (void)updateRows
{
    int i;
    for (i = 0; i < [rows count]; ++i) {
        NSDictionary *choice = [visibleChoices objectAtIndex:i];
        NSSet *names = [NSSet setWithArray:[choice objectForKey:@"Packages"]];
        BOOL done = [names isSubsetOfSet:installed];
        NSButton *row = [rows objectAtIndex:i];
        NSString *title = [choice objectForKey:@"Title"];
        [row setState:done || [names intersectsSet:selected]];
        [row setEnabled:!busy && !done];
        [row setTitle:done ? [title stringByAppendingString:@" (Installed)"] : title];
    }
    [installButton setEnabled:!busy && ([selected count] != 0 || [self needsPIC])];
    [quitButton setTitle:busy ? @"Stop" : @"Quit"];
    [quitButton setEnabled:!busy || !stopRequested];
}
- (void)show
{
    int i;
    float height = 0, y;
    NSString *category = nil;
    NSScrollView *scroll;
    NSView *content;
    window = [[NSWindow alloc] initWithContentRect:NSMakeRect(80, 60, 460, 430)
        styleMask:NSTitledWindowMask | NSMiniaturizableWindowMask
        backing:NSBackingStoreBuffered defer:NO];
    [window setTitle:@"OPENSTEP Setup"];
    [self label:@"Select additional software to install.\nComplete each installation, then quit Installer; Setup opens the next package automatically."
               frame:NSMakeRect(14, 362, 432, 54) bold:NO in:[window contentView]];
    for (i = 0; i < [visibleChoices count]; ++i) {
        NSString *next = [[visibleChoices objectAtIndex:i] objectForKey:@"Category"];
        height += [next isEqualToString:category] ? 22 : 44;
        category = next;
    }
    height = MAX(height, 245); y = height - 22; category = nil;
    scroll = [[[NSScrollView alloc] initWithFrame:NSMakeRect(14, 110, 432, 245)] autorelease];
    [scroll setHasVerticalScroller:YES];
    content = [[[NSView alloc] initWithFrame:NSMakeRect(0, 0, 410, height)] autorelease];
    for (i = 0; i < [visibleChoices count]; ++i) {
        NSDictionary *choice = [visibleChoices objectAtIndex:i];
        NSButton *row;
        if (![[choice objectForKey:@"Category"] isEqualToString:category]) {
            category = [choice objectForKey:@"Category"];
            [self label:category frame:NSMakeRect(6, y, 400, 19) bold:YES in:content]; y -= 22;
        }
        row = [[[NSButton alloc] initWithFrame:NSMakeRect(14, y, 396, 20)] autorelease];
        [row setButtonType:NSSwitchButton]; [row setFont:[NSFont systemFontOfSize:12]];
        [row setImagePosition:NSImageLeft]; [row setAlignment:NSLeftTextAlignment];
        [row setTarget:self]; [row setAction:@selector(selectionChanged:)];
        [content addSubview:row]; [rows addObject:row]; y -= 22;
    }
    [scroll setDocumentView:content]; [[window contentView] addSubview:scroll];
    [content scrollPoint:NSMakePoint(0, height - 245)];
    status = [self label:@"" frame:NSMakeRect(14, 57, 432, 44) bold:NO in:[window contentView]];
    quitButton = [[NSButton alloc] initWithFrame:NSMakeRect(244, 15, 94, 28)];
    [quitButton setTitle:@"Quit"]; [quitButton setTarget:self]; [quitButton setAction:@selector(quit:)];
    [[window contentView] addSubview:quitButton];
    installButton = [[NSButton alloc] initWithFrame:NSMakeRect(342, 15, 104, 28)];
    [installButton setTitle:@"Install"]; [installButton setTarget:self]; [installButton setAction:@selector(install:)];
    [[window contentView] addSubview:installButton];
    [self updateRows];
    [self message:[available count] ? @"Ready. Already-installed packages will be skipped." : @"No supported packages are present on this CD."];
    [window center]; [window makeKeyAndOrderFront:self];
    [NSApp activateIgnoringOtherApps:YES];
}
- (void)selectionChanged:(id)sender
{
    int i;
    NSMutableSet *requested = [NSMutableSet set], *missing = [NSMutableSet set];
    NSArray *work;
    for (i = 0; i < [rows count]; ++i) {
        NSButton *row = [rows objectAtIndex:i];
        if ([row state] && [row isEnabled])
            [requested addObjectsFromArray:[[visibleChoices objectAtIndex:i] objectForKey:@"Packages"]];
    }
    work = [model plan:requested available:available installed:installed missing:missing];
    if ([missing count]) {
        NSMutableString *message = [NSMutableString stringWithString:@"Required packages are neither installed nor on this CD:\n"];
        for (i = 0; i < [work count]; ++i)
            if ([missing containsObject:[work objectAtIndex:i]]) [message appendFormat:@"\n%@", [work objectAtIndex:i]];
        [self alert:message];
    } else {
        [selected setSet:[NSSet setWithArray:work]]; [requested minusSet:installed];
        if (![selected isEqualToSet:requested])
            [self message:@"Required packages have been selected. Uncheck their dependents first to remove them."];
    }
    [self updateRows];
}
- (void)pause:(NSString *)reason
{
    busy = NO; [active release]; active = nil;
    [timer invalidate]; timer = nil;
    [beforeReceipt release]; beforeReceipt = nil;
    [candidateReceipt release]; candidateReceipt = nil;
    [self scanInstalled]; [selected minusSet:installed];
    [installButton setTitle:@"Retry"];
    [self updateRows]; [self message:reason];
    [self alert:[NSString stringWithFormat:@"%@\n\nLog: %@", reason, logPath]];
}
- (void)startPIC
{
    NSString *script = [cdRoot stringByAppendingPathComponent:@"NextCD/fix-pic-bug"];
    if (![[NSFileManager defaultManager] fileExistsAtPath:script]) {
        [self pause:@"The CD's PIC helper is missing. The kernel has not been verified; do not reboot yet."]; return;
    }
    picTask = [[NSTask alloc] init]; picOutput = [[NSPipe pipe] retain];
    [picTask setLaunchPath:@"/usr/bin/perl"];
    [picTask setArguments:[NSArray arrayWithObjects:script, @"/mach_kernel", nil]];
    [picTask setStandardOutput:picOutput]; [picTask setStandardError:picOutput];
    [self message:@"Applying the PIC fix to the installed kernel..."];
    NS_DURING
        [picTask launch];
    NS_HANDLER
        [picTask release]; picTask = nil; [picOutput release]; picOutput = nil;
        [self pause:@"Could not launch the PIC helper. Do not reboot before fixing the kernel."];
    NS_ENDHANDLER
}
- (void)advance
{
    NSString *path;
    if ([self needsPIC]) { [self startPIC]; return; }
    if (stopRequested) [pending removeAllObjects];
    if ([installerTask isRunning]) {
        [self message:stopRequested ? @"Quit Installer to finish stopping Setup." :
            [pending count] ? @"Installation complete. Quit Installer to open the next selected package." :
            @"Installation complete. Quit Installer to finish Setup."];
        return;
    }
    [installerTask release]; installerTask = nil;
    if (![pending count]) {
        busy = NO; [timer invalidate]; timer = nil;
        [self scanInstalled]; [selected minusSet:installed];
        [installButton setTitle:@"Install"]; [self updateRows];
        [self message:stopRequested ? @"Stopped. Remaining packages were not started." : @"Selected packages are installed."];
        [self alert:[NSString stringWithFormat:@"%@%@\n\nLog: %@",
            stopRequested ? @"Setup stopped after the current operation." : @"Setup is complete.",
            restartNeeded ? @"\nRestart the computer to use the installed patches." : @"", logPath]];
        return;
    }
    active = [[pending objectAtIndex:0] retain];
    path = [packageRoot stringByAppendingPathComponent:[active stringByAppendingString:@".pkg"]];
    if (![[NSFileManager defaultManager] fileExistsAtPath:path]) {
        [self pause:@"The selected package is no longer available. Check that the CD is mounted."]; return;
    }
    [beforeReceipt release]; beforeReceipt = [receiptStamp([self receipt:active], active) retain];
    [candidateReceipt release]; candidateReceipt = nil;
    [self message:[NSString stringWithFormat:@"Launching Installer for %@ (uid %lu, euid %lu)",
        active, (unsigned long)getuid(), (unsigned long)geteuid()]];
    installerTask = newInstallerTask(path);
    NS_DURING
        launchInstallerTask(installerTask);
    NS_HANDLER
        [installerTask release]; installerTask = nil;
        [self pause:[NSString stringWithFormat:@"Could not launch /NextAdmin/Installer.app/Installer: %@. No later package was started.",
            [localException reason]]];
    NS_ENDHANDLER
}
- (void)install:(id)sender
{
    NSMutableSet *missing = [NSMutableSet set];
    if (busy) return;
    [self scanInstalled];
    [pending release];
    pending = [[model plan:selected available:available installed:installed missing:missing] mutableCopy];
    if ([missing count]) { [self alert:@"A prerequisite is missing. Review the package selection."]; return; }
    [selected setSet:[NSSet setWithArray:pending]];
    busy = YES; stopRequested = NO;
    [self updateRows];
    timer = [NSTimer scheduledTimerWithTimeInterval:0.5 target:self selector:@selector(poll:) userInfo:nil repeats:YES];
    [self advance];
}
- (BOOL)receiptReady:(BOOL)exited
{
    NSDictionary *stamp = [self matchingReceipt:active];
    if (!stamp || [stamp isEqual:beforeReceipt]) {
        [candidateReceipt release]; candidateReceipt = nil; return NO;
    }
    if (exited || [stamp isEqual:candidateReceipt]) return YES;
    [candidateReceipt release]; candidateReceipt = [stamp retain];
    return NO;
}
- (void)completedPackage
{
    NSDictionary *package = [model package:active];
    [self message:[NSString stringWithFormat:@"Installed %@", active]];
    [installed addObject:active]; [pending removeObjectAtIndex:0]; [selected removeObject:active];
    if ([[package objectForKey:@"FixPICAfter"] isEqual:@"YES"]) picDone = NO;
    if ([[package objectForKey:@"RestartRequired"] isEqual:@"YES"]) restartNeeded = YES;
    [active release]; active = nil;
    [beforeReceipt release]; beforeReceipt = nil;
    [candidateReceipt release]; candidateReceipt = nil;
    [self advance];
}
- (void)poll:(NSTimer *)sender
{
    if (!busy) return;
    if (picTask) {
        if (![picTask isRunning]) {
            int code = [picTask terminationStatus];
            NSData *data = [[picOutput fileHandleForReading] readDataToEndOfFile];
            NSString *output = [[[NSString alloc] initWithData:data encoding:NSNEXTSTEPStringEncoding] autorelease];
            if (log && output) { fprintf(log, "%s\n", [output cString]); fflush(log); }
            [picTask release]; picTask = nil; [picOutput release]; picOutput = nil;
            if (code != 0) {
                [self pause:[NSString stringWithFormat:@"PIC patching failed. Do not reboot yet.\n%@", output]];
            } else { picDone = YES; [self message:@"Kernel PIC fix verified."]; [self advance]; }
        }
    } else if (active) [self checkInstaller];
    else if (![installerTask isRunning]) [self advance];
}
- (void)checkInstaller
{
    BOOL exited = ![installerTask isRunning];
    if ([self receiptReady:exited]) { [self completedPackage]; return; }
    if (!exited) return;
    [installerTask release]; installerTask = nil;
    [self pause:@"Installer closed without a new successful receipt. Retry or quit; no later package was started."];
}
- (void)quit:(id)sender
{
    if (busy) {
        stopRequested = YES;
        [self updateRows];
        if (picTask) [self message:@"Stopping after the kernel PIC check finishes..."];
        else {
            [self message:@"No further packages will open. Finish or cancel the current operation in Installer, then quit Installer."];
            if (active) [self checkInstaller];
            else [self advance];
        }
    } else [NSApp terminate:self];
}
- (BOOL)applicationShouldTerminate:(NSApplication *)application
{
    if (!busy) return YES;
    [self alert:@"An installation or kernel patch is still active. Use Stop; finish or cancel the current installation in Installer. Setup must finish the PIC check before exiting."];
    return NO;
}
- (void)dealloc
{
    [timer invalidate];
    if (log) fclose(log);
    [model release]; [available release]; [installed release]; [selected release];
    [rows release]; [visibleChoices release]; [pending release]; [active release];
    [window release]; [quitButton release]; [installButton release];
    [cdRoot release]; [packageRoot release]; [logPath release];
    [beforeReceipt release]; [candidateReceipt release];
    [picTask release]; [picOutput release]; [installerTask release];
    [super dealloc];
}
@end

int main(int argc, char **argv)
{
    NSAutoreleasePool *pool = [[NSAutoreleasePool alloc] init];
    SetupController *controller;
    [NSApplication sharedApplication];
    controller = [[SetupController alloc] init];
    [NSApp setDelegate:controller];
    if ([controller prepare]) { [controller show]; [NSApp run]; }
    [NSApp setDelegate:nil]; [controller release]; [pool release];
    return 0;
}
