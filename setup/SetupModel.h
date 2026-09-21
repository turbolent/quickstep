#import <Foundation/Foundation.h>

@interface SetupModel : NSObject
{
    NSArray *packages, *choices, *order;
    NSDictionary *byName;
    BOOL fixPIC;
}
- (id)initWithPropertyList:(id)plist error:(NSString **)error;
- (NSArray *)packages;
- (NSArray *)choices;
- (NSDictionary *)package:(NSString *)name;
- (BOOL)fixPIC;
- (BOOL)choiceVisible:(NSDictionary *)choice available:(NSSet *)available installed:(NSSet *)installed;
- (NSArray *)plan:(NSSet *)selected available:(NSSet *)available installed:(NSSet *)installed
          missing:(NSMutableSet *)missing;
@end
