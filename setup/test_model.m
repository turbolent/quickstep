#import "SetupModel.h"
#include <assert.h>
#include <stdio.h>

static NSSet *names(NSArray *packages, unsigned mask)
{
    NSMutableSet *result = [NSMutableSet set];
    unsigned i;
    for (i = 0; i < [packages count]; ++i)
        if (mask & (1U << i)) [result addObject:[[packages objectAtIndex:i] objectForKey:@"Name"]];
    return result;
}

static void invalid(id plist)
{
    NSString *error = nil;
    SetupModel *model = [[SetupModel alloc] initWithPropertyList:plist error:&error];
    assert(!model && [error length]);
}

int main(int argc, char **argv)
{
    NSAutoreleasePool *pool = [[NSAutoreleasePool alloc] init];
    NSDictionary *plist = [NSDictionary dictionaryWithContentsOfFile:@"test_catalog.plist"];
    NSString *error = nil;
    SetupModel *model = [[SetupModel alloc] initWithPropertyList:plist error:&error];
    NSMutableSet *missing = [NSMutableSet set];
    NSArray *packages, *queue;
    unsigned selectedMask, installedMask, availableMask;
    NSMutableDictionary *changed, *package;
    NSMutableArray *many;
    int i;
    assert(model && !error && [model fixPIC]);
    packages = [model packages];
    queue = [model plan:[NSSet setWithObject:@"Patch"] available:names(packages, 15)
               installed:[NSSet set] missing:missing];
    assert(([queue isEqual:[NSArray arrayWithObjects:@"Tools", @"Libs", @"Base", @"Patch", nil]]));
    for (selectedMask = 0; selectedMask < 16; ++selectedMask)
        for (installedMask = 0; installedMask < 16; ++installedMask)
            for (availableMask = 0; availableMask < 16; ++availableMask) {
                NSSet *selected = names(packages, selectedMask), *installed = names(packages, installedMask);
                NSSet *available = names(packages, availableMask);
                NSMutableSet *completed = [NSMutableSet setWithSet:installed], *needed;
                queue = [model plan:selected available:available installed:installed missing:missing];
                needed = [NSMutableSet setWithArray:queue];
                assert(![needed intersectsSet:installed]);
                [needed minusSet:available]; assert([needed isEqualToSet:missing]);
                for (i = 0; i < [queue count]; ++i) {
                    NSString *name = [queue objectAtIndex:i];
                    NSSet *dependencies = [NSSet setWithArray:[[model package:name] objectForKey:@"Dependencies"]];
                    assert([dependencies isSubsetOfSet:completed]);
                    [completed addObject:name];
                }
                assert([selected isSubsetOfSet:completed]);
            }
    assert(![model choiceVisible:[[model choices] objectAtIndex:1]
                      available:[NSSet setWithObject:@"Tools"] installed:[NSSet set]]);
    assert([model choiceVisible:[[model choices] objectAtIndex:1]
                     available:[NSSet setWithObject:@"Tools"] installed:[NSSet setWithObject:@"Libs"]]);
    invalid(nil); invalid(@"not a dictionary");
    /* A post-install action is an argv array, never an implicit shell command. */
    for (i = 0; i < 3; ++i) {
        changed = [[plist mutableCopy] autorelease];
        package = [[[packages objectAtIndex:0] mutableCopy] autorelease];
        [package setObject:i == 0 ? (id)@"/bin/true" : i == 1 ?
            (id)[NSArray arrayWithObject:@"relative"] : (id)[NSArray arrayWithObjects:@"/bin/sh", @"", nil]
                    forKey:@"PostInstall"];
        many = [[packages mutableCopy] autorelease]; [many replaceObjectAtIndex:0 withObject:package];
        [changed setObject:many forKey:@"Packages"]; invalid(changed);
    }
    changed = [[plist mutableCopy] autorelease];
    [changed setObject:@"2" forKey:@"FormatVersion"]; invalid(changed);
    [changed setObject:@"1" forKey:@"FormatVersion"];
    [changed setObject:[NSArray arrayWithObjects:[packages objectAtIndex:0], [packages objectAtIndex:0], nil]
               forKey:@"Packages"]; invalid(changed);
    for (i = 0; i < 3; ++i) {
        package = [[[packages objectAtIndex:0] mutableCopy] autorelease];
        [package setObject:[NSArray arrayWithObject:i == 0 ? @"Missing" : @"Patch"] forKey:@"Dependencies"];
        if (i == 2) [package setObject:@"../unsafe" forKey:@"Name"];
        [changed setObject:[NSArray arrayWithObjects:package, [packages objectAtIndex:1],
                           [packages objectAtIndex:2], [packages objectAtIndex:3], nil] forKey:@"Packages"];
        invalid(changed);
    }
    /* A reversed chain longer than a machine-word bitmask. */
    many = [NSMutableArray array];
    for (i = 39; i >= 0; --i) {
        package = [[[packages objectAtIndex:0] mutableCopy] autorelease];
        [package setObject:[NSString stringWithFormat:@"P%d", i] forKey:@"Name"];
        [package setObject:i ? [NSArray arrayWithObject:[NSString stringWithFormat:@"P%d", i - 1]] : [NSArray array]
                   forKey:@"Dependencies"];
        [many addObject:package];
    }
    [changed setObject:many forKey:@"Packages"]; [changed setObject:[NSArray array] forKey:@"Choices"];
    [model release]; model = [[SetupModel alloc] initWithPropertyList:changed error:&error];
    assert(model && !error);
    queue = [model plan:[NSSet setWithObject:@"P39"] available:[NSSet set] installed:[NSSet set] missing:missing];
    assert([queue count] == 40 && [missing count] == 40);
    for (i = 0; i < 40; ++i) assert(([[queue objectAtIndex:i] isEqual:[NSString stringWithFormat:@"P%d", i]]));
    [model release];
    /* Optionally check a plist emitted by the real Python builder. */
    if (argc == 2) {
        plist = [NSDictionary dictionaryWithContentsOfFile:[NSString stringWithCString:argv[1]]];
        model = [[SetupModel alloc] initWithPropertyList:plist error:&error];
        assert(model && !error);
        if ([model package:@"FramebufferWC"]) {
            NSSet *available = [NSSet setWithObjects:@"OS42MachUserPatch4", @"FramebufferWC", nil];
            queue = [model plan:[NSSet setWithObject:@"FramebufferWC"] available:available
                       installed:[NSSet set] missing:missing];
            assert(([queue isEqual:[NSArray arrayWithObjects:@"OS42MachUserPatch4", @"FramebufferWC", nil]]));
            [model plan:[NSSet setWithObject:@"FramebufferWC"] available:[NSSet setWithObject:@"FramebufferWC"]
              installed:[NSSet set] missing:missing];
            assert([missing isEqual:[NSSet setWithObject:@"OS42MachUserPatch4"]]);
        }
        [model release];
    }
    puts("Setup catalog tests passed.");
    [pool release];
    return 0;
}
