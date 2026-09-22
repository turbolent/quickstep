# Run with GNU make on OPENSTEP 4.2 with Developer Tools and Libraries installed.
CC = cc
CFLAGS = -arch i386 -O -Wall
APP = build/Setup.app
APP_ICON = /usr/lib/NextStep/Workspace.app/application.tiff
# Workspace identifies GUI applications through the executable's __ICON data.
APP_ICON_FLAGS = -sectcreate __ICON __header Setup.iconheader \
	-sectcreate __ICON app $(APP_ICON) -segprot __ICON r r

all: $(APP)/Setup $(APP)/setup-framebuffer-wc.sh

$(APP)/setup-framebuffer-wc.sh: setup-framebuffer-wc.sh
	/bin/mkdirs $(APP)
	/bin/cp setup-framebuffer-wc.sh $@
	/bin/rm -f $(APP)/setup-framebuffer-wc.pl

$(APP)/Setup: main.m SetupModel.m SetupModel.h Setup.iconheader $(APP_ICON) Makefile
	/bin/mkdirs $(APP)
	$(CC) $(CFLAGS) $(APP_ICON_FLAGS) -o $@ main.m SetupModel.m -framework AppKit -framework Foundation

test: $(APP)/Setup build/test_model build/test_controller
	./build/test_model
	./build/test_controller

build/test_model: test_model.m SetupModel.m SetupModel.h
	/bin/mkdirs build
	$(CC) $(CFLAGS) -o $@ test_model.m SetupModel.m -framework Foundation

build/test_controller: test_controller.m main.m SetupModel.m SetupModel.h
	/bin/mkdirs build
	$(CC) $(CFLAGS) -o $@ test_controller.m SetupModel.m -framework AppKit -framework Foundation

release: all test
	cd build && /usr/bin/gnutar cf Setup-OPENSTEP-i386.tar Setup.app

.PHONY: all test release
