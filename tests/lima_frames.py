from pymavlink.dialects.v20 import ardupilotmega as mavlink


def frame(msg, sysid=1, compid=1, seq=0, v1=False):
    mav = mavlink.MAVLink(None, srcSystem=sysid, srcComponent=compid)
    mav.seq = seq
    return msg.pack(mav, force_mavlink1=v1)


def decode(data):
    mav = mavlink.MAVLink(None)
    mav.robust_parsing = True
    return [m for m in mav.parse_buffer(data) or [] if m.get_type() != "BAD_DATA"]
