CC ?= cc
CFLAGS ?= -O3 -Wall -Wextra -std=c11
PKG_CONFIG ?= pkg-config
SRCDIR := src
INCDIR := include
BINDIR := bin
BUILDDIR := build

CFITSIO_CFLAGS := $(shell $(PKG_CONFIG) --cflags cfitsio 2>/dev/null)
CFITSIO_LIBS := $(shell $(PKG_CONFIG) --libs cfitsio 2>/dev/null)

UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
  OMP_CFLAGS ?= -Xpreprocessor -fopenmp -I/opt/homebrew/opt/libomp/include
  OMP_LIBS ?= -L/opt/homebrew/opt/libomp/lib -lomp
else
  OMP_CFLAGS ?= -fopenmp
  OMP_LIBS ?= -fopenmp
endif

INC_FLAGS := -I$(INCDIR)
COMPILE_FLAGS := $(CFLAGS) $(OMP_CFLAGS) $(CFITSIO_CFLAGS) $(INC_FLAGS)
LINK_FLAGS := $(CFITSIO_LIBS) $(OMP_LIBS) -lm

TARGETS := $(BINDIR)/lnc_registered_pair $(BINDIR)/lnc_unregistered_pair $(BINDIR)/lnc_group_subs
LNC_CORE_SRCS := $(addprefix $(SRCDIR)/, lnc_common.c lnc_fits.c lnc_transform.c lnc_grid.c lnc_fit.c lnc_unregistered_core.c lnc_pair_process.c)
LNC_CORE_OBJS := $(patsubst $(SRCDIR)/%.c,$(BUILDDIR)/%.o,$(LNC_CORE_SRCS))
LNC_HEADERS := $(wildcard $(INCDIR)/*.h)
REGISTERED_OBJS := \
	$(BUILDDIR)/lnc_registered_pair_main.o \
	$(BUILDDIR)/lnc_registered_trimmed_mean.o \
	$(BUILDDIR)/lnc_registered_trimmed_median.o \
	$(BUILDDIR)/lnc_registered_sample_median.o \
	$(BUILDDIR)/lnc_common.o \
	$(BUILDDIR)/lnc_fits.o

.PHONY: all clean lnc_registered_pair lnc_unregistered_pair lnc_group_subs

all: $(TARGETS)

lnc_registered_pair: $(BINDIR)/lnc_registered_pair
lnc_unregistered_pair: $(BINDIR)/lnc_unregistered_pair
lnc_group_subs: $(BINDIR)/lnc_group_subs

$(BINDIR) $(BUILDDIR):
	mkdir -p $@

$(BUILDDIR)/%.o: $(SRCDIR)/%.c $(LNC_HEADERS) | $(BUILDDIR)
	$(CC) $(COMPILE_FLAGS) -c -o $@ $<

$(BUILDDIR)/lnc_registered_trimmed_mean.o: $(SRCDIR)/lnc_registered_standard.c | $(BUILDDIR)
	$(CC) $(COMPILE_FLAGS) -DLNC_REGISTERED_STANDARD_MAIN=lnc_registered_trimmed_mean_main -c -o $@ $<

$(BUILDDIR)/lnc_registered_trimmed_median.o: $(SRCDIR)/lnc_registered_standard.c | $(BUILDDIR)
	$(CC) $(COMPILE_FLAGS) -DLNC_USE_TRIMMED_MEDIAN -DLNC_BACKGROUND_ESTIMATOR=\"trimmed-median\" -DLNC_REGISTERED_STANDARD_MAIN=lnc_registered_trimmed_median_main -c -o $@ $<

$(BINDIR)/lnc_registered_pair: $(REGISTERED_OBJS) | $(BINDIR)
	$(CC) $(CFLAGS) $(OMP_CFLAGS) -o $@ $(REGISTERED_OBJS) $(LINK_FLAGS)

$(BINDIR)/lnc_unregistered_pair: $(BUILDDIR)/lnc_unregistered_pair_main.o $(LNC_CORE_OBJS) | $(BINDIR)
	$(CC) $(CFLAGS) $(OMP_CFLAGS) -o $@ $^ $(LINK_FLAGS)

$(BINDIR)/lnc_group_subs: $(BUILDDIR)/lnc_group_subs_main.o $(LNC_CORE_OBJS) | $(BINDIR)
	$(CC) $(CFLAGS) $(OMP_CFLAGS) -o $@ $^ $(LINK_FLAGS)

clean:
	rm -rf $(BINDIR) $(BUILDDIR)
