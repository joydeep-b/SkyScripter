all: indi_cam_client test_client

indi_cam_client: indi_cam_client.cc
	g++ -o indi_cam_client indi_cam_client.cc -lindiclient -lgflags -I /usr/include/libindi

test_client: test_client.cc
	g++ -o test_client test_client.cc -lindiclient -lgflags -I /usr/include/libindi

clean:
	rm -f indi_cam_client