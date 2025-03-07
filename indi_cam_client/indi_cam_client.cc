#include <signal.h>

#include "libindi/baseclient.h"
#include "libindi/basedevice.h"
#include "libindi/indiccd.h"
#include "libindi/indilogger.h"

#include "gflags/gflags.h"

#include <string>
#include <iostream>

using std::string;

// INDI server settings.
DEFINE_string(server, "localhost", "INDI server hostname");
DEFINE_int32(port, 7624, "INDI server port");
// INDI Camera settings.
DEFINE_string(device, "QHY CCD QHY268M-b93fd94", "INDI device name");
DEFINE_string(ccd_blob_name, "CCD1", "Name of the CCD blob property");
DEFINE_double(exposure, 1.0, "Exposure time in seconds");
DEFINE_int32(mode, 0, "CCD read mode");
DEFINE_int32(gain, 0, "CCD gain");
DEFINE_int32(offset, 0, "CCD offset");
// Program settings.
DEFINE_int32(timeout, 1, "Timeout in seconds while waiting for INDI properties");
DEFINE_string(output, "image.fits", "Output filename");
DEFINE_int32(v, 0, "Verbosity level");

class CameraClient : public INDI::BaseClient {
public:

  void newProperty(INDI::Property property) override {
    if (strcmp(property.getBaseDevice().getDeviceName(), FLAGS_device.c_str()) != 0) {
      if (FLAGS_v > 1) printf("Ignoring device %s\n", property.getBaseDevice().getDeviceName());
      return;
    }
    if (strcmp(property.getName(), "CCD_EXPOSURE") == 0) {
      exposureElement = property.getNumber();
    } else if (strcmp(property.getName(), "READ_MODE") == 0) {
      INDI::PropertyViewNumber* nvp = property.getNumber();
      INDI::WidgetViewNumber* wvn = nvp->at(0);
      if (FLAGS_v > 0) printf("Setting read mode to %d\n", FLAGS_mode);
      wvn->value = FLAGS_mode;
      sendNewNumber(nvp);
    } else if (strcmp(property.getName(), "CCD_GAIN") == 0) {
      INDI::PropertyViewNumber* nvp = property.getNumber();
      INDI::WidgetViewNumber* wvn = nvp->at(0);
      if (FLAGS_v > 0) printf("Setting gain to %d\n", FLAGS_gain);
      wvn->value = FLAGS_gain;
      sendNewNumber(nvp);
    } else if (strcmp(property.getName(), "CCD_OFFSET") == 0) {
      INDI::PropertyViewNumber* nvp = property.getNumber();
      INDI::WidgetViewNumber* wvn = nvp->at(0);
      if (FLAGS_v > 0) printf("Setting offset to %d\n", FLAGS_offset);
      wvn->value = FLAGS_offset;
      sendNewNumber(nvp);
    }
  }

  void updateProperty(INDI::Property property) override {
    if (FLAGS_v > 0 && strcmp(property.getName(), "CCD_EXPOSURE") == 0) {
      INDI::PropertyViewNumber* nvp = property.getNumber();
      INDI::WidgetViewNumber* wvn = nvp->at(0);
      printf("CCD_EXPOSURE = %7.3f\n", wvn->value);
    }
    if (property.getType() == INDI_BLOB) {
      INDI::PropertyViewBlob* bvp = property.getBLOB();
      if (strcmp(bvp->name, FLAGS_ccd_blob_name.c_str()) != 0) {
        if (FLAGS_v > 1) printf("Ignoring BLOB from %s\n", bvp->name);
        return;
      }
      if (bvp->nbp < 1) {
        fprintf(stderr, "Received BLOB with no blobs\n");
        exit(1);
      }
      INDI::WidgetViewBlob* bp = bvp->at(0);
      if (FLAGS_v > 0) {
        printf("Received camera image:\n Label=%s\n Name=%s\n Format=%s\n Size=%d\n",
            bp->label, bp->name, bp->format, bp->bloblen);
        printf("Saving to %s\n", FLAGS_output.c_str());
      }
      FILE *fp = fopen(FLAGS_output.c_str(), "w");
      if (!fp) {
        fprintf(stderr, "Failed to open %s\n", FLAGS_output.c_str());
        exit(1);
      }
      fwrite(bp->blob, bp->bloblen, 1, fp);
      fclose(fp);
      exit(0);
    } else if (property.getType() == INDI_NUMBER) {
      INDI::PropertyViewNumber* nvp = property.getNumber();
      INDI::WidgetViewNumber* wvn = nvp->at(0);
      if (FLAGS_v > 1) printf("Received number %s = %f\n", property.getName(), wvn->value);
      if (strcmp(property.getName(), "READ_MODE") == 0) {
        mode = wvn->value;
      } else if (strcmp(property.getName(), "CCD_GAIN") == 0) {
        gain = wvn->value;
      } else if (strcmp(property.getName(), "CCD_OFFSET") == 0) {
        offset = wvn->value;
      }
    }
  }

  void CaptureImage() {
    int timeout = 10 * FLAGS_timeout;
    bool ready = exposureElement &&
        mode == FLAGS_mode &&
        gain == FLAGS_gain &&
        offset == FLAGS_offset;
    for (int i = 0; !ready && i < timeout; i++) {
      usleep(1e5);
      ready = exposureElement &&
          mode == FLAGS_mode &&
          gain == FLAGS_gain &&
          offset == FLAGS_offset;
    }
    if (!ready) {
      std::cerr << "ERROR: Timeout waiting to update device properties" << std::endl;
      exit(1);
    }
    INDI::WidgetViewNumber* wvn = exposureElement->at(0);
    if (!wvn) {
      std::cerr << "Exposure element is null" << std::endl;
      exit(1);
    }
    if (FLAGS_v > 0) printf("Setting exposure to %f\n", FLAGS_exposure);
    wvn->value = FLAGS_exposure;
    sendNewNumber(exposureElement);
  }

private:
  INDI::PropertyViewNumber* exposureElement = nullptr;
  int mode = -1;
  int gain = -1;
  int offset = -1;
};

int main(int argc, char** argv) {
  gflags::ParseCommandLineFlags(&argc, &argv, true);
  CameraClient client;
  client.setServer(FLAGS_server.c_str(), FLAGS_port);

  // INDI::BaseClient::connectServer() is stupid and pollutes stderr with debug messages that can't
  // be turned off, and even more stupidly it does not print any usefull logs when there is an
  // actual error, e.g., if the server or port are invalid. Redirect stderr to a file to avoid this.
  const int saved_stdout_fd = dup(STDERR_FILENO);
  FILE *fp = fopen("/dev/null", "w");
  dup2(fileno(fp), STDERR_FILENO);
  fclose(fp);
  const bool connected = client.connectServer();
  // Restore original stderr.
  dup2(saved_stdout_fd, STDERR_FILENO);
  close(saved_stdout_fd);
  if (!connected) {
    fprintf(stderr, "Failed to connect to INDI server %s:%d\n", FLAGS_server.c_str(), FLAGS_port);
    return 1;
  }

  client.setBLOBMode(B_ALSO, FLAGS_device.c_str(), FLAGS_ccd_blob_name.c_str());
  if (FLAGS_v > 0) printf("Connected to INDI server %s:%d\n", FLAGS_server.c_str(), FLAGS_port);
  client.CaptureImage();
  while (1) {
    usleep(1e5);
  }
  return 0;
}