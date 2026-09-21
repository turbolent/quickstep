#import "SetupModel.h"
#include <string.h>

static BOOL string(id value)
{
    return [value isKindOfClass:[NSString class]] && [value length] != 0;
}

static BOOL flag(id value)
{
    return [value isKindOfClass:[NSString class]] &&
        ([value isEqualToString:@"YES"] || [value isEqualToString:@"NO"]);
}

static BOOL packageName(id name)
{
    const char *bytes;
    if (!string(name) || [name isEqualToString:@"."] || [name isEqualToString:@".."]) return NO;
    bytes = [name cString];
    return strspn(bytes, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.+-") == strlen(bytes);
}

static BOOL references(id names, NSDictionary *packages)
{
    unsigned i;
    if (![names isKindOfClass:[NSArray class]]) return NO;
    for (i = 0; i < [names count]; ++i)
        if (!string([names objectAtIndex:i]) || ![packages objectForKey:[names objectAtIndex:i]]) return NO;
    return YES;
}

@implementation SetupModel
- (id)initWithPropertyList:(id)plist error:(NSString **)error
{
    NSMutableDictionary *index = [NSMutableDictionary dictionary];
    NSMutableArray *sorted = [NSMutableArray array];
    NSMutableSet *resolved = [NSMutableSet set];
    NSString *problem = @"Invalid Setup.plist header.";
    unsigned i, previous;
    self = [super init];
    if (!self) return nil;
    if (![plist isKindOfClass:[NSDictionary class]] ||
        ![[plist objectForKey:@"FormatVersion"] isEqual:@"1"] || !flag([plist objectForKey:@"FixPICBug"])) goto invalid;
    fixPIC = [[plist objectForKey:@"FixPICBug"] isEqual:@"YES"];
    if (![[plist objectForKey:@"Packages"] isKindOfClass:[NSArray class]] ||
        ![[plist objectForKey:@"Choices"] isKindOfClass:[NSArray class]]) goto invalid;
    packages = [[plist objectForKey:@"Packages"] copy];
    choices = [[plist objectForKey:@"Choices"] copy];
    problem = @"Invalid or duplicate package in Setup.plist.";
    for (i = 0; i < [packages count]; ++i) {
        NSDictionary *package = [packages objectAtIndex:i];
        NSString *name;
        if (![package isKindOfClass:[NSDictionary class]]) goto invalid;
        name = [package objectForKey:@"Name"];
        if (!packageName(name) || [index objectForKey:name] || !string([package objectForKey:@"Version"]) ||
            !flag([package objectForKey:@"Relocatable"]) || !flag([package objectForKey:@"RestartRequired"]) ||
            !flag([package objectForKey:@"FixPICAfter"])) goto invalid;
        [index setObject:package forKey:name];
    }
    problem = @"Unknown or invalid dependency in Setup.plist.";
    for (i = 0; i < [packages count]; ++i)
        if (!references([[packages objectAtIndex:i] objectForKey:@"Dependencies"], index)) goto invalid;
    problem = @"Invalid choice or unknown package in Setup.plist.";
    for (i = 0; i < [choices count]; ++i) {
        NSDictionary *choice = [choices objectAtIndex:i];
        if (![choice isKindOfClass:[NSDictionary class]] || !string([choice objectForKey:@"Category"]) ||
            !string([choice objectForKey:@"Title"]) || !flag([choice objectForKey:@"DefaultSelected"]) ||
            !references([choice objectForKey:@"Packages"], index) || ![[choice objectForKey:@"Packages"] count]) goto invalid;
    }
    /* Stable topological order: the plist decides the order among ready packages. */
    problem = @"Cycle in Setup.plist package dependencies.";
    while ([sorted count] < [packages count]) {
        previous = [sorted count];
        for (i = 0; i < [packages count]; ++i) {
            NSDictionary *package = [packages objectAtIndex:i];
            NSString *name = [package objectForKey:@"Name"];
            NSSet *dependencies = [NSSet setWithArray:[package objectForKey:@"Dependencies"]];
            if (![resolved containsObject:name] && [dependencies isSubsetOfSet:resolved]) {
                [sorted addObject:name]; [resolved addObject:name];
                break;
            }
        }
        if ([sorted count] == previous) goto invalid;
    }
    byName = [index copy]; order = [sorted copy];
    if (error) *error = nil;
    return self;
invalid:
    if (error) *error = problem;
    [self release];
    return nil;
}
- (NSArray *)packages { return packages; }
- (NSArray *)choices { return choices; }
- (NSDictionary *)package:(NSString *)name { return [byName objectForKey:name]; }
- (BOOL)fixPIC { return fixPIC; }
- (BOOL)choiceVisible:(NSDictionary *)choice available:(NSSet *)available installed:(NSSet *)installed
{
    NSSet *names = [NSSet setWithArray:[choice objectForKey:@"Packages"]];
    NSMutableSet *present = [NSMutableSet setWithSet:available];
    [present unionSet:installed];
    return [names intersectsSet:available] && [names isSubsetOfSet:present];
}
- (NSArray *)plan:(NSSet *)selected available:(NSSet *)available installed:(NSSet *)installed
          missing:(NSMutableSet *)missing
{
    NSMutableSet *pending = [NSMutableSet setWithSet:selected];
    NSMutableArray *queue = [NSMutableArray array];
    int i;
    [pending minusSet:installed];
    /* Walking backwards expands each dependency before we reach it. */
    for (i = (int)[order count] - 1; i >= 0; --i) {
        NSString *name = [order objectAtIndex:i];
        if ([pending containsObject:name]) {
            [pending addObjectsFromArray:[[byName objectForKey:name] objectForKey:@"Dependencies"]];
            [pending minusSet:installed];
        }
    }
    [missing setSet:pending]; [missing minusSet:available];
    for (i = 0; i < [order count]; ++i)
        if ([pending containsObject:[order objectAtIndex:i]]) [queue addObject:[order objectAtIndex:i]];
    return queue;
}
- (void)dealloc
{
    [packages release]; [choices release]; [order release]; [byName release];
    [super dealloc];
}
@end
